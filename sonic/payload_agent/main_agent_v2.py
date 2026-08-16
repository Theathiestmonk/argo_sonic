"""main_agent_v2.py — payload-driven Sonic agent.

Wake word -> empty Payload -> loop:
  - is_complete_for_intent(): execute(), speak the result, session ends.
  - get_next_question() == "navigate": drive there (no listen), loop.
  - get_next_question() == "describe_category": speak the category
    (announcement, no listen), mark it described, loop.
  - get_next_question() is None (intent still NONE): speak the short
    wake-ack and listen — the reply classifies intent.
  - otherwise: speak the question, listen, extract, merge, advance, loop.

Reuses main_agent.py's audio/nav/wake-word I/O (STT, TTS, mic recording,
wake-word detection, Nav2 bridge) via module-attribute access — that
infrastructure is unchanged; only the dialogue state machine here is new.
Run directly: python3 main_agent_v2.py [--text-mode] [--test-mode]
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import main_agent as sonic_v1  # noqa: E402 — proven STT/TTS/wake-word/nav I/O, reused as-is

import db  # noqa: E402
import executor  # noqa: E402
import menu_loader  # noqa: E402
from extraction import extract  # noqa: E402
from llm_client import LLMClient  # noqa: E402
from models import Intent, Payload  # noqa: E402
from payload_manager import advance, mark_category_described, merge, resolve_item_changes  # noqa: E402
from prompt_generator import render_announcement, render_question  # noqa: E402

llm = LLMClient()

FORCED_LOCATION = os.environ.get("TABLE_NO") or None
FORCED_ACTION = os.environ.get("SONIC_ACTION_HINT") or None
DISPATCH_ACTION_TO_INTENT = {
    "order": Intent.TAKE_ORDER,
    "room_service": Intent.TAKE_ORDER,
    "bill": Intent.GET_BILL,
    "deliver": Intent.DELIVER_ORDER,
}

# Context each question/announcement needs beyond what render_*() already
# pulls from the payload itself (order summary, table, etc. are read
# straight off ctx kwargs by the {placeholder} in prompt_generator's hints).
QUESTION_CONTEXT = {
    "item_qty": lambda p: {"item_name": (p._last_item().name if p._last_item() else "that")},
    "anything_else": lambda p: {"order_summary": p.order_summary()},
    "confirm_order": lambda p: {
        "order_summary": p.order_summary(),
        "total": f"{menu_loader.CURRENCY_SYMBOL}{sum((menu_loader.get_price(i.name) or 0) * (i.qty or 0) for i in p.order_items.values()):.2f}",
    },
    "menu_category": lambda p: {"categories": ", ".join(menu_loader.get_categories())},
}


def speak(text: str) -> None:
    """Decoupled from main_agent.py's speak_text() — that one logs into
    OrderState/_current_state, which Payload doesn't have. History logging
    here happens explicitly at each call site instead (append_history)."""
    print(f"Sonic: {text}")
    if sonic_v1.TEXT_MODE:
        return
    sentences = sonic_v1.split_sentences(text)
    if not sentences:
        return
    if sonic_v1.sarvam_tts_stream(sentences):
        return
    for sentence in sentences:
        try:
            audio, sr = sonic_v1.sarvam_tts(sentence)
            sonic_v1.play_audio(audio, sr)
        except Exception as e:
            print(f"[warn] TTS failed for a chunk: {e}")


def listen_with_patience(prompt_text: str) -> Optional[str]:
    """Same patient-silence pattern as main_agent.py's listen_with_patience,
    just built on this module's own speak() instead of speak_text()."""
    speak(prompt_text)
    start = time.monotonic()
    while True:
        remaining = sonic_v1.SILENCE_GIVEUP_TOTAL_S - (time.monotonic() - start)
        if remaining <= 0:
            return None
        reply = sonic_v1.listen(timeout_s=min(sonic_v1.SILENCE_ONSET_TIMEOUT_S, remaining))
        if reply:
            return reply
        if reply is None and (time.monotonic() - start) < sonic_v1.SILENCE_GIVEUP_TOTAL_S:
            speak(prompt_text)


def dispatch_init_payload() -> Optional[Payload]:
    """Mirrors main_agent.py's FORCED_LOCATION/FORCED_ACTION fast path —
    the dashboard's "Take Order"/"Bill"/"Deliver" button sets TABLE_NO/
    SONIC_ACTION_HINT before spawning this process."""
    if not (FORCED_LOCATION and FORCED_ACTION):
        return None
    intent = DISPATCH_ACTION_TO_INTENT.get(FORCED_ACTION)
    if intent is None:
        return None
    return Payload(intent=intent, order_table=int(FORCED_LOCATION), robot_location=0)


def run_session() -> None:
    payload = dispatch_init_payload() or Payload()

    while True:
        if payload.is_complete_for_intent():
            text = executor.execute(llm, payload)
            speak(text)
            return

        question = payload.get_next_question()

        if question == "navigate":
            destination = f"Table {payload.order_table}"
            arrived = sonic_v1.navigate_and_wait(destination)
            if not arrived:
                speak(render_announcement(llm, "nav_failed", payload))
                return
            payload.robot_location = payload.order_table
            continue

        if question == "describe_category":
            desc = menu_loader.describe_category(payload.current_menu_category)
            speak(render_announcement(llm, "menu_description", payload,
                                       category=payload.current_menu_category, items=desc))
            mark_category_described(payload)
            continue

        if question is None:
            # intent is still NONE — nothing to ask about yet; a short
            # wake-ack invites the guest to say what they want.
            transcript = listen_with_patience(render_announcement(llm, "wake_ack", payload))
            expects = "intent_and_table"
        else:
            ctx = QUESTION_CONTEXT.get(question, lambda p: {})(payload)
            prompt = render_question(llm, question, payload, **ctx)
            transcript = listen_with_patience(prompt)
            expects = question

        if transcript is None:
            speak(render_announcement(llm, "farewell", payload))
            return

        raw = extract(llm, transcript, payload, expects)
        item_named = any(c.get("name") for c in raw["item_changes"])
        changes = resolve_item_changes(payload, raw["item_changes"], canonicalize=menu_loader.canonical_name)
        payload = merge(payload, {**raw, "order_items": changes})
        payload = advance(payload, item_named_this_turn=item_named)
        payload.conversation_history.append({"role": "user", "text": transcript})


def main() -> None:
    parser = argparse.ArgumentParser(description="Sonic main_agent_v2 — payload-driven voice agent")
    parser.add_argument("--text-mode", action="store_true",
                        help="Typed input/printed output instead of mic/speaker/wake-word")
    parser.add_argument("--test-mode", action="store_true",
                        help="Skip real Nav2 trips (instant-arrival) — everything else stays real")
    args = parser.parse_args()

    sonic_v1.TEXT_MODE = args.text_mode
    sonic_v1.SONIC_SKIP_NAV = sonic_v1.SONIC_SKIP_NAV or args.test_mode

    if not args.text_mode:
        sonic_v1.require_api_keys()
    if sonic_v1.SONIC_SKIP_NAV:
        print("[main] test mode — navigation will be skipped (instant arrival), everything else is real")

    db.resolve_robot()
    menu_loader.load_menu()

    if FORCED_LOCATION:
        print(f"[main] TABLE_NO={FORCED_LOCATION!r} SONIC_ACTION_HINT={FORCED_ACTION!r} — dispatched session")
        run_session()
        return

    if args.text_mode:
        print("=== main_agent_v2.py (text mode) — no mic, no wake word, no Sarvam calls ===")
        while True:
            run_session()
    else:
        oww_model = sonic_v1.load_wake_word_model()
        print(f"main_agent_v2 is listening for the wake word ('{sonic_v1.WAKE_WORD_NAME}')...")
        while True:
            sonic_v1.wait_for_wake_word(oww_model)
            run_session()


if __name__ == "__main__":
    main()
