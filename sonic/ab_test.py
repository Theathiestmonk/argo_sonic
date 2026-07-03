"""
ab_test.py
----------
Run this to A/B test sentence-chunk vs word-by-word streaming yourself.

Usage:
    python ab_test.py

It will ask you a few test phrases and run them through both modes
back to back so you can directly compare how Sonic sounds and how
fast the first audio comes through.
"""

import sys
import time
import mode_sentence
import mode_word


TEST_PHRASES = [
    "Hey Sonic, how are you doing today?",
    "Can you tell me a quick joke?",
    "What can you do for me around the house?",
    "Are you smarter than Alexa?",
]


def print_divider(label=""):
    print("\n" + "─" * 60)
    if label:
        print(f"  {label}")
        print("─" * 60)


def run_comparison(phrase: str):
    print_divider(f"TESTING: \"{phrase}\"")

    print("\n  🔵 MODE 1 — Sentence-chunk streaming (recommended)")
    result_sentence = mode_sentence.run(phrase)

    time.sleep(1)  # small gap so you can tell the modes apart by ear

    print("\n  🟠 MODE 2 — Word-by-word streaming (sequential playback)")
    result_word = mode_word.run(phrase, playback="sequential")

    print_divider("COMPARISON RESULTS")
    print(f"  Sentence-chunk : first audio in {result_sentence['time_to_first_audio']:.2f}s, "
          f"total {result_sentence['total_time']:.2f}s, "
          f"{result_sentence['sentence_count']} sentence(s) spoken")
    print(f"  Word-by-word   : first audio in {result_word['time_to_first_audio']:.2f}s, "
          f"total {result_word['total_time']:.2f}s, "
          f"{result_word['word_count']} word(s) spoken")
    print()


def main():
    print("""
  ╔══════════════════════════════════════════════════════════╗
  ║   Sonic Streaming A/B Test — Sentence vs Word-by-word     ║
  ╚══════════════════════════════════════════════════════════╝

  This will run the same phrases through both streaming modes
  so you can compare audio quality and perceived speed yourself.

  Listen for:
    - How natural/choppy each mode sounds
    - How quickly you hear the FIRST sound after asking
    - Whether word-by-word has awkward gaps between words

  Type a custom phrase, or press Enter to use the test set.
    """)

    custom = input("  Custom phrase (or Enter for defaults): ").strip()

    if custom:
        run_comparison(custom)
    else:
        for phrase in TEST_PHRASES:
            run_comparison(phrase)
            cont = input("\n  Press Enter for next test, or 'q' to quit: ").strip().lower()
            if cont == "q":
                break

    print("\n  Test complete. Pick whichever mode sounded better to you,")
    print("  then use that mode's file (mode_sentence.py or mode_word.py)")
    print("  in your main Sonic project.\n")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n  Test interrupted. Bye!\n")
        sys.exit(0)
