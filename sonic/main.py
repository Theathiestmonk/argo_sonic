"""
main.py
-------
Sonic — full voice loop with:
  - Scenario selection at startup (restaurant / hotel / bar / home)
  - Scenario-locked fast matcher (only that scenario's commands match)
  - General commands always active across all scenarios
  - 5-second follow-up window after each response — if user doesn't
    say anything, Sonic goes back to wake-word mode quietly
  - Fixed responses for known commands — LLM never overrides these
  - LLM only for open-ended conversation and unknown questions

Run:
    python main.py
"""

import datetime
import re
import sys
import time

import requests

import config
import fast_matcher
import llm_voice
import menu_client
import wake_word
import mic_stt
from conversation import ConversationContext
from dispatcher import dispatch, get_robot_state
from tts import speak

# Cues that end order-taking with no new items in the same utterance — e.g.
# "one burger, that's all" is handled as an item (via menu_client) BEFORE this
# ever gets checked; this only fires when nothing on the menu matched.
_CLOSING_CUE_RE = re.compile(
    r"\b(that'?s (all|it|everything)|nothing else|no more|done|finished|that'?ll be (it|all))\b"
)

# Checked only while context.awaiting_confirmation is True (i.e. the customer
# already heard the order read back) — a plain "yes"/"correct" etc. confirms
# it same as repeating a closing cue like "done" would.
_CONFIRM_CUE_RE = re.compile(
    r"\b(yes|yeah|yep|yup|confirm|confirmed|correct|that'?s right|sure|go ahead)\b"
)

# Checked BEFORE treating a mentioned item as an addition — otherwise "remove
# the burger" would silently add another burger instead of taking it off the
# order (the exact bug this fixes). Deliberately doesn't include "cancel" —
# that already fast-matches the general CANCEL/CANCEL_ORDER intents in
# fast_matcher.py before this code ever runs, so adding it here would just
# be unreachable dead weight.
_REMOVE_CUE_RE = re.compile(
    r"\b(remove|delete|don'?t want|take (that |it )?off|without)\b"
)

# "What did I order" / "read my order back" etc. — reads the cart back
# without the price (see _speak_order_list) unless _TOTAL_CUE_RE also
# matches in the same utterance ("what's my order and the total").
_ORDER_LIST_CUE_RE = re.compile(
    r"\b(what'?s (in )?my order|what did i order|read (back )?my order|"
    r"order so far|what have i ordered|my order (so far|list))\b"
)
# Checked on its own too, for when the customer only asks for the amount
# ("what's my total", "how much do I owe") without asking for the item
# list — and, combined with _ORDER_LIST_CUE_RE matching too, for a request
# that asks for both in one breath ("what's my order and the total").
# Bare "total" is deliberately enough on its own: it's not a menu word this
# gets checked against (this runs before any menu matching), so there's no
# real dish name it'd collide with.
_TOTAL_CUE_RE = re.compile(
    r"\b(total|how much (is it|do i owe|does it cost))\b"
)

# A customer asking what to get, with no specific dish named — either
# generally ("what's good", "suggest something") or scoped to a protein/diet
# they mentioned ("chicken", "veg", "non veg"). menu_client.py's
# _GENERIC_HEAD_NOUNS deliberately keeps these bare words from resolving to
# one arbitrary dish (e.g. "chicken" silently becoming "Butter Chicken" just
# because it's the only item ending in that word) — this is where they get
# handled properly instead, as a real suggestion request.
_SUGGEST_CUE_RE = re.compile(
    r"\b(suggest|recommend|what'?s good|any recommendation|what should i (get|order|have)|what do you have)\b"
)
_DIET_CUES = [
    (re.compile(r"\bnon[\s-]?veg(etarian)?\b"), "nonveg"),
    (re.compile(r"\bchicken\b"), "nonveg"),
    (re.compile(r"\bmutton\b"), "nonveg"),
    (re.compile(r"\bfish\b"), "nonveg"),
    (re.compile(r"\bpaneer\b"), "veg"),
    (re.compile(r"\bveg(etarian)?\b"), "veg"),
]


def _detect_diet_cue(text_lower: str) -> str | None:
    for pattern, diet in _DIET_CUES:
        if pattern.search(text_lower):
            return diet
    return None


# ── Banner ─────────────────────────────────────────────────────────────────

def print_banner():
    print("""
  ███████╗ ██████╗ ███╗   ██╗██╗ ██████╗
  ██╔════╝██╔═══██╗████╗  ██║██║██╔════╝
  ███████╗██║   ██║██╔██╗ ██║██║██║
  ╚════██║██║   ██║██║╚██╗██║██║██║
  ███████║╚██████╔╝██║ ╚████║██║╚██████╗
  ╚══════╝ ╚═════╝ ╚═╝  ╚═══╝╚═╝ ╚═════╝

  Say "Hey Sonic" to start. Ctrl+C to exit.
    """)


# ── Startup ─────────────────────────────────────────────────────────────────

def startup_checks():
    print("  [Startup] Checking services...\n")
    # Missing entirely (no .env / wrong var name) or still the placeholder —
    # either way, fail with a clear message instead of a confusing crash or
    # a live API call that 401s later.
    if (not config.GROQ_API_KEY or config.GROQ_API_KEY.startswith("YOUR_")
            or not config.SARVAM_API_KEY or config.SARVAM_API_KEY.startswith("YOUR_")):
        print("  ⚠️  Missing API keys — create a sonic/.env file (see sonic/README.md) with:\n"
              "      GROQ_API_KEY=...\n"
              "      SARVAM_API_KEY=...\n")
        sys.exit(1)

    ww_model = wake_word.load_model()
    mic_ok = mic_stt.check_dependencies()

    if ww_model and mic_ok:
        print("\n  ✅ Full voice mode — wake word + mic ready.\n")
        return "voice", ww_model
    else:
        print("\n  ⚠️  Mic/wake word unavailable — keyboard fallback mode.\n")
        return "keyboard", None


# ── Scenario selection ───────────────────────────────────────────────────────

def select_scenario_voice(ww_model) -> str:
    """
    Ask the user which scenario to activate — via voice.
    Keeps asking until a valid scenario is confirmed.
    """
    while True:
        speak(config.SCENARIO_PROMPT)

        # Wait for wake word, then record the answer
        wake_word.listen_for_wake_word(ww_model)
        user_text = mic_stt.listen_and_transcribe()

        if not user_text:
            speak("Sorry, I didn't catch that.")
            continue

        scenario = fast_matcher.detect_scenario_from_text(user_text)
        if scenario:
            speak(config.SCENARIO_CONFIRMED[scenario])
            print(f"\n  ✅ Scenario locked: {scenario.upper()}\n")
            return scenario

        speak(
            "Sorry, I didn't catch which scenario. "
            "Please say restaurant, hotel, bar, or home."
        )


def select_scenario_keyboard() -> str:
    """Keyboard fallback for scenario selection."""
    speak(config.SCENARIO_PROMPT)
    print("\n  Options: restaurant / hotel / bar / home")

    while True:
        choice = input("\n  Select scenario: ").strip().lower()
        scenario = fast_matcher.detect_scenario_from_text(choice)
        if scenario:
            speak(config.SCENARIO_CONFIRMED[scenario])
            print(f"\n  ✅ Scenario locked: {scenario.upper()}\n")
            return scenario
        print("  Not recognised — type: restaurant, hotel, bar, or home")


# ── Order-taking (restaurant) ────────────────────────────────────────────────

def _speak_order_update(added: list, rejected: list, ambiguous: list,
                         menu_items: list) -> str:
    """Only called when added/rejected/ambiguous has at least one entry —
    see _handle_order_turn's own "nothing matched" message for the empty
    case. Rejections and ambiguous mentions both get category-aware
    suggestions from menu_client.category_alternatives() instead of a flat
    "not available"/"didn't catch that" dead end — a customer who can't
    have what they asked for can usually be pointed at something similar.

    Deliberately doesn't read the running total back here — the price is
    only spoken once, in the order readback after the customer says "done"
    (see _order_readback), not after every single item."""
    parts = []
    if added:
        desc = ", ".join(f"{m['qty']} {m['name']}" for m in added)
        parts.append(f"Added {desc}.")

    for m in rejected:
        line = f"Sorry, {m['name']} isn't available right now."
        alts = menu_client.category_alternatives(m.get("category"), menu_items, exclude_ids={m["id"]})
        if alts:
            line += f" We do have {' and '.join(a['name'] for a in alts)} though!"
        parts.append(line)

    for group in ambiguous:
        names = " or ".join(c["name"] for c in group)
        parts.append(f"We have {names} — which would you like?")

    # An ambiguous group already ends the turn on a question awaiting an
    # answer — don't pile a second one on top of it. Otherwise always
    # prompt for what's next instead of just going quiet, since that quiet
    # is what used to leave the customer unsure whether Sonic was still
    # listening at all.
    if not ambiguous:
        parts.append("Anything else, or are you done?")
    return " ".join(parts)


def _speak_order_list(context: ConversationContext, with_total: bool) -> str:
    """Reads the cart back item-by-item — no price unless with_total (set
    when _TOTAL_CUE_RE also matched the same utterance, see
    _handle_order_turn) — so "what's my order" doesn't force a total
    recap the customer didn't ask for."""
    if context.cart.is_empty():
        return "You haven't ordered anything yet."
    desc = ", ".join(f"{it['qty']} {it['name']}" for it in context.cart.items)
    response = f"So far you have {desc}."
    if with_total:
        response += f" Your total is {context.cart.total():.2f}."
    return response


def _handle_removal(matched: list, ambiguous: list, context: ConversationContext) -> str:
    """Called instead of adding, once _REMOVE_CUE_RE fires — see
    _handle_order_turn. matched/ambiguous come from the same
    menu_client.find_menu_items_in_text() call used for additions.

    "qty_explicit" (None unless the customer actually said a number) is what
    decides partial vs. whole-line removal — "remove the biryani" takes all
    of it off, but "remove 2 biryani" out of 3 on the order only takes 2 off
    and leaves 1, instead of always dropping the whole line regardless of
    how many were actually there before."""
    if not matched and not ambiguous:
        return "You don't have anything matching that in your order yet."

    parts = []
    for group in ambiguous:
        names = " or ".join(c["name"] for c in group)
        parts.append(f"Did you mean {names}? Say the full name and I'll remove it.")

    removed_all, removed_partial, missing = [], [], []
    for m in matched:
        line = next((l for l in context.cart.items if l["id"] == m["id"]), None)
        if line is None:
            missing.append(m["name"])
            continue
        qty = m.get("qty_explicit")
        if qty is not None and qty < line["qty"]:
            remaining = line["qty"] - qty  # read before remove() mutates line["qty"] in place
            context.cart.remove(m["id"], qty)
            removed_partial.append(f"{qty} {m['name']} (you have {remaining} left)")
        else:
            context.cart.remove(m["id"])
            removed_all.append(m["name"])

    if removed_all:
        parts.append(f"Removed {', '.join(removed_all)}.")
    if removed_partial:
        parts.append(f"Removed {', '.join(removed_partial)}.")
    for name in missing:
        parts.append(f"You didn't have {name} in your order.")

    if (removed_all or removed_partial) and context.cart.is_empty():
        parts.append("Your order is now empty.")
    elif not context.cart.is_empty():
        parts.append(f"Your total so far is {context.cart.total():.2f}.")
    return " ".join(parts)


def _speak_suggestions(suggestions: list, diet: str | None) -> str:
    """Called when nothing matched as a specific dish but the customer asked
    for a recommendation — generally, or scoped to a protein/diet they named
    (see _detect_diet_cue). suggestions comes from menu_client.suggest_items()."""
    if not suggestions:
        return "Sorry, I don't have a specific recommendation right now — what would you like to try?"
    names = " or ".join(s["name"] for s in suggestions)
    qualifier = {"veg": " vegetarian", "nonveg": " non-veg"}.get(diet, "")
    return f"How about{qualifier} {names}? They're quite popular!"


def _order_readback(context: ConversationContext) -> str:
    """Called once, when the customer first signals they're done (see
    _CLOSING_CUE_RE in _handle_order_turn) — reads the cart back so a
    mis-heard item can be caught before it ever reaches the kitchen, and
    sets awaiting_confirmation so the NEXT turn (a plain "yes"/"done"/etc.,
    see _CONFIRM_CUE_RE) submits instead of reading it back again."""
    context.awaiting_confirmation = True
    desc = ", ".join(f"{it['qty']} {it['name']}" for it in context.cart.items)
    return f"You have {desc} — your total is {context.cart.total():.2f}. Shall I confirm your order?"


def _finalize_order(context: ConversationContext) -> str:
    """Ends order-taking and, if there's a table/map to post to (i.e. this
    run came from the test harness, not a bare `python main.py` dev session),
    saves the finished order to backend/launcher.py's per-table order file."""
    context.taking_order = False
    context.awaiting_confirmation = False
    if context.cart.is_empty():
        return "Alright, no items were ordered."

    order = context.cart.to_dict()
    order["status"] = "submitted"
    order["updatedAt"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    total = order["total"]

    if context.table_id and context.map_name:
        try:
            requests.post(
                f"{config.LAUNCHER_URL}/orders/{context.map_name}/{context.table_id}",
                json=order, timeout=3,
            )
            print(f"  [Order] Saved to backend: table={context.table_id} map={context.map_name}")
        except requests.RequestException as e:
            print(f"  [Order] Could not reach backend to save order: {e}")
    else:
        print("  [Order] No table_id/map_name set on this context — skipping backend "
              "POST (expected for a plain `python main.py` dev run without the harness).")

    context.cart.clear()
    return f"Your order is confirmed! Total is {total:.2f}. I'll get that ready for you!"


def _handle_order_turn(user_text: str, context: ConversationContext) -> bool:
    """Called only while context.taking_order is True, before the LLM fallback.
    Returns True if this turn was fully handled here (caller skips the LLM)."""
    if not context.taking_order:
        return False

    text_lower = user_text.lower()
    menu_items = menu_client.get_menu()
    context.add_user(user_text)

    # The customer already heard the order read back (see _order_readback)
    # and this turn is their answer. A confirm/closing cue ("yes", "done",
    # etc.) submits the order; anything else — another item, "no", "wait" —
    # falls out of this block and is handled normally below instead, so
    # e.g. "actually add fries too" still adds fries rather than being
    # swallowed as a non-confirmation.
    if context.awaiting_confirmation:
        if _CONFIRM_CUE_RE.search(text_lower) or _CLOSING_CUE_RE.search(text_lower):
            response = _finalize_order(context)
            speak(response)
            context.add_assistant(response)
            return True
        context.awaiting_confirmation = False

    # "What's my order" / "what's my total" etc. — checked before menu
    # matching so a bare "order" in the phrase never gets treated as an
    # item mention (menu_client._GENERIC_HEAD_NOUNS already blocks that too,
    # this is just belt-and-suspenders for the exact wording customers use).
    if _ORDER_LIST_CUE_RE.search(text_lower):
        response = _speak_order_list(context, with_total=bool(_TOTAL_CUE_RE.search(text_lower)))
        speak(response)
        context.add_assistant(response)
        return True

    if _TOTAL_CUE_RE.search(text_lower):
        response = (f"Your total so far is {context.cart.total():.2f}."
                    if not context.cart.is_empty() else "You haven't ordered anything yet.")
        speak(response)
        context.add_assistant(response)
        return True

    # Checked BEFORE treating any match as an addition — "remove the burger"
    # would otherwise just add another burger (matched purely on mentioning
    # the dish, same as an order would be).
    if _REMOVE_CUE_RE.search(text_lower):
        matched, ambiguous = menu_client.find_menu_items_in_text(user_text, menu_items)
        response = _handle_removal(matched, ambiguous, context)
        speak(response)
        context.add_assistant(response)
        return True

    matches, ambiguous = menu_client.find_menu_items_in_text(user_text, menu_items)
    added = [m for m in matches if m["available"]]
    rejected = [m for m in matches if not m["available"]]

    if added or rejected or ambiguous:
        for m in added:
            context.cart.add(m["id"], m["name"], m["qty"], m["price"])
        response = _speak_order_update(added, rejected, ambiguous, menu_items)
        speak(response)
        context.add_assistant(response)
        return True

    # No specific dish matched — check for a suggestion request before
    # giving up: either general ("what's good") or scoped to a protein/diet
    # word the customer said (menu_client._GENERIC_HEAD_NOUNS keeps bare
    # words like "chicken" from resolving to one arbitrary dish, so this is
    # where they get handled properly instead).
    diet = _detect_diet_cue(text_lower)
    if diet or _SUGGEST_CUE_RE.search(text_lower):
        suggestions = menu_client.suggest_items(menu_items, diet=diet, limit=3)
        response = _speak_suggestions(suggestions, diet)
        speak(response)
        context.add_assistant(response)
        return True

    if _CLOSING_CUE_RE.search(text_lower):
        # Nothing to confirm if the cart's empty — just end order-taking,
        # no readback needed. Otherwise read it back and wait for the next
        # turn's confirm cue (handled by the awaiting_confirmation check
        # above) instead of submitting on this first "done".
        response = _finalize_order(context) if context.cart.is_empty() else _order_readback(context)
        speak(response)
        context.add_assistant(response)
        return True

    # Nothing on the menu matched (including a genuinely ambiguous mention),
    # no removal, no suggestion request, no closing cue either. Deliberately
    # do NOT fall through to the LLM here: it would happily invent a
    # friendly-sounding confirmation ("Two milkshakes coming up!") for an
    # item that was never actually added to the cart, which is worse than
    # asking the customer to repeat themselves.
    response = "Sorry, I didn't catch an item from the menu — could you say that again?"
    speak(response)
    context.add_assistant(response)
    return True


# ── Turn processing ─────────────────────────────────────────────────────────

def process_turn(user_text: str, context: ConversationContext,
                 active_scenario: str) -> None:
    """
    Run one full pipeline turn.
    Fast matcher is checked FIRST and ALWAYS wins — it returns fixed
    responses from commands.py with zero LLM involvement.
    LLM is only called when no known command pattern matches.
    """
    # Fast lane — scenario-aware: only matches active scenario + general
    fast_result = fast_matcher.try_fast_match(user_text, active_scenario)

    if fast_result:
        intent   = fast_result["intent"]
        slots    = fast_result["slots"]
        response = fast_result["response"]
        action   = fast_result["action"]
        after    = fast_result["after_dialog"]

        print(f"  ⚡ [{intent}] fast match — fixed response, no LLM")

        context.set_scenario(fast_result["scenario"])
        context.update_slots(slots)
        context.last_intent = intent
        context.add_user(user_text)
        context.add_assistant(response)

        speak(response)

        if intent == "TAKE_ORDER":
            context.taking_order = True

        if action and action != "none":
            # Run the physical action — this may take several seconds
            # (navigation travel, kitchen handoff, delivery sequence, etc.)
            status = dispatch(intent, slots, context)
            print(f"  [Dispatch] {status}")

            # after_dialog speaks AFTER the action is fully done
            # e.g. "I've arrived." after navigate_to completes
            #      "Order confirmed with the kitchen!" after take_order returns
            if after:
                time.sleep(0.3)   # tiny breath between action and after-line
                speak(after)
                context.add_assistant(after)

        if intent == "REQUEST_BILL" and context.taking_order:
            # Customer asked for the bill mid-order (fast-matches REQUEST_BILL
            # regardless of taking_order state) — treat it as the order's
            # closing cue too, same finalize path as saying "that's all".
            time.sleep(0.3)
            finalize_response = _finalize_order(context)
            speak(finalize_response)
            context.add_assistant(finalize_response)

    else:
        if _handle_order_turn(user_text, context):
            print(f"\n  {context.summary()}")
            return

        # Smart lane — general conversation or unknown question
        print("  🤔 No command match — sending to Groq (general conversation)...")
        context.add_user(user_text)

        result = llm_voice.run(
            user_text,
            conversation_history=context.trimmed_history(max_turns=8),
            active_scenario=active_scenario,
        )

        context.add_assistant(result["full_reply"])
        print(f"  ⏱️  First audio: {result['time_to_first_audio']:.2f}s  "
              f"Total: {result['total_time']:.2f}s")

    print(f"\n  {context.summary()}")


# ── Voice loop ───────────────────────────────────────────────────────────────

def voice_loop(ww_model, context: ConversationContext, active_scenario: str):
    """
    Main voice loop.

    STATE MACHINE:
      IDLE  → waiting for "Hey Sonic" (wake word)
      AWAKE → user spoke, process the turn
      FOLLOW_UP → wait MIC_FOLLOW_UP_WINDOW seconds for the user to say
                  something else without needing "Hey Sonic" again
                  If silence → go back to IDLE (wake word mode)
    """
    print(f"  👂 Waiting for wake word in {active_scenario.upper()} mode...\n")

    while True:
        try:
            # ── IDLE: wait for wake word ─────────────────────────────────
            wake_word.listen_for_wake_word(ww_model)
            speak("Yes?")

            # ── First turn ───────────────────────────────────────────────
            user_text = mic_stt.listen_and_transcribe()

            if not user_text:
                speak("I didn't catch that — say Hey Sonic when you need me!")
                continue

            if _is_goodbye(user_text):
                speak("Goodbye!")
                break

            process_turn(user_text, context, active_scenario)
            print("\n  " + "─" * 55)

            # ── FOLLOW-UP: stay awake for MIC_FOLLOW_UP_WINDOW seconds ──
            while True:
                print(f"\n  ⏳ Follow-up window "
                      f"({config.MIC_FOLLOW_UP_WINDOW:.0f}s)...")

                follow_text = mic_stt.listen_continued()

                if not follow_text:
                    # Timed out — user is done
                    print("  💤 No follow-up — returning to wake word mode.")
                    print(f"\n  👂 Waiting for wake word in "
                          f"{active_scenario.upper()} mode...\n")
                    break   # ← back to outer IDLE loop

                if _is_goodbye(follow_text):
                    speak("Goodbye!")
                    return

                process_turn(follow_text, context, active_scenario)
                print("\n  " + "─" * 55)
                # loop again — keep follow-up window open

        except KeyboardInterrupt:
            speak("Shutting down. Goodbye!")
            break


def voice_loop_order_session(context: ConversationContext, active_scenario: str):
    """
    Like voice_loop(), but for a session Sonic itself already opened by
    speaking first (test_harness.py's arrival announcement, "Are you ready
    to order?") — so it must NOT wait for "Hey Sonic" before listening for
    the customer's reply. That wake phrase is for getting Sonic's attention
    in the first place; a customer answering a question Sonic just asked
    them doesn't say it again, and never hearing that was exactly why
    nothing happened after the arrival line got spoken.

    Ends the session once the order is no longer being taken (finalized via
    a closing cue or a bill request, see main.py's _finalize_order) instead
    of looping back to wake-word-idle mode — this is a single bounded table
    visit, not the general always-on assistant loop.
    """
    while context.taking_order:
        try:
            user_text = mic_stt.listen_and_transcribe()
            if not user_text:
                # Nothing heard — with items already on the order this reads
                # as "gone quiet while deciding", not "didn't understand", so
                # prompt for what's next instead of asking them to repeat an
                # order they haven't necessarily said anything wrong in.
                if context.cart.is_empty():
                    speak("Sorry, I didn't catch that — could you repeat your order?")
                else:
                    speak("Anything else, or are you done?")
                continue
            if _is_goodbye(user_text):
                speak("Goodbye!")
                return
            process_turn(user_text, context, active_scenario)
            print("\n  " + "─" * 55)
        except KeyboardInterrupt:
            speak("Shutting down. Goodbye!")
            return


# ── Keyboard loop (fallback) ─────────────────────────────────────────────────

def keyboard_loop(context: ConversationContext, active_scenario: str):
    """Keyboard fallback — used when mic/wake word hardware isn't available."""
    print(f"  Ready in {active_scenario.upper()} mode. Type your message.\n")
    print("  Commands: state | history | reset | quit\n")
    print("  " + "─" * 55)

    while True:
        try:
            user_text = input("\n  You: ").strip()
            if not user_text:
                continue

            lower = user_text.lower()
            if lower in ("quit", "exit", "q"):
                print("  Sonic: Goodbye!")
                break
            if lower == "state":
                for k, v in get_robot_state().items():
                    print(f"    {k}: {v}")
                continue
            if lower == "history":
                for m in context.history[-10:]:
                    print(f"    {'You' if m['role']=='user' else 'Sonic'}: {m['content']}")
                continue
            if lower == "reset":
                context.reset()
                print("  Context cleared.")
                continue

            process_turn(user_text, context, active_scenario)
            print("\n  " + "─" * 55)

        except KeyboardInterrupt:
            print("\n\n  Sonic: Goodbye!\n")
            break


# ── Helpers ──────────────────────────────────────────────────────────────────

def _is_goodbye(text: str) -> bool:
    t = text.lower()
    return any(kw in t for kw in ["goodbye sonic", "bye sonic", "shut down", "shut sonic"])


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    print_banner()
    mode, ww_model = startup_checks()

    context = ConversationContext()

    if mode == "voice":
        active_scenario = select_scenario_voice(ww_model)
        voice_loop(ww_model, context, active_scenario)
    else:
        active_scenario = select_scenario_keyboard()
        keyboard_loop(context, active_scenario)


if __name__ == "__main__":
    main()
