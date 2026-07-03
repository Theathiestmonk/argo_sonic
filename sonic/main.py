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

import sys
import time

import config
import fast_matcher
import llm_voice
import wake_word
import mic_stt
from conversation import ConversationContext
from dispatcher import dispatch, get_robot_state
from tts import speak


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
    if config.GROQ_API_KEY.startswith("YOUR_") or config.SARVAM_API_KEY.startswith("YOUR_"):
        print("  ⚠️  Please fill in API keys in config.py\n")
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

    else:
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
