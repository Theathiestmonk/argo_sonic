"""
mode_word.py
------------
WORD-BY-WORD streaming mode.

Sends each individual word to TTS the moment it arrives from Groq.
This minimizes time-to-first-audio but will sound choppy/robotic since
Sarvam TTS loses sentence-level prosody when given isolated words.

Two playback strategies are included so you can compare:
  - SEQUENTIAL: each word's audio plays fully before the next starts
                (no overlap, but words queue up if generation outpaces playback)
  - OVERLAP:    fires audio async, can overlap/garble if Groq is faster
                than Sarvam can generate+play audio for each word
"""

import json
import time
import requests
import config
from tts import speak


def run(user_text: str, conversation_history: list = None, playback: str = "sequential") -> dict:
    """
    Run one full turn: Groq streaming → per-word Sarvam TTS calls.
    playback: "sequential" (blocking, safe) or "overlap" (async, riskier)
    Returns timing stats for comparison against sentence-chunk mode.
    """
    history = conversation_history or []
    messages = [{"role": "system", "content": config.SONIC_SYSTEM_PROMPT}]
    messages += history
    messages.append({"role": "user", "content": user_text})

    print(f"\n  You: {user_text}")
    t_start = time.time()
    t_first_audio = None
    word_count = 0

    response = requests.post(
        config.GROQ_URL,
        headers={"Authorization": f"Bearer {config.GROQ_API_KEY}"},
        json={
            "model": config.GROQ_MODEL,
            "messages": messages,
            "stream": True,
            "temperature": config.GROQ_TEMPERATURE,
            "max_tokens": config.GROQ_MAX_TOKENS,
        },
        stream=True,
        timeout=30,
    )

    buffer = ""
    full_reply = ""
    block = (playback == "sequential")

    for line in response.iter_lines():
        if not line or not line.startswith(b"data: "):
            continue
        chunk = line[6:]
        if chunk == b"[DONE]":
            break

        try:
            delta = json.loads(chunk)["choices"][0]["delta"].get("content", "")
        except (KeyError, json.JSONDecodeError, IndexError):
            continue

        if not delta:
            continue

        buffer += delta
        full_reply += delta

        # flush on whitespace = a complete word is ready
        if " " in buffer:
            parts = buffer.split(" ")
            word = parts[0]
            buffer = " ".join(parts[1:])

            if word.strip():
                if t_first_audio is None:
                    t_first_audio = time.time() - t_start
                    print(f"  ⏱️  Time to first audio: {t_first_audio:.2f}s")

                speak(word.strip(), block=block)
                word_count += 1

    if buffer.strip():
        if t_first_audio is None:
            t_first_audio = time.time() - t_start
        speak(buffer.strip(), block=block)
        word_count += 1

    t_total = time.time() - t_start

    return {
        "mode": f"word_by_word_{playback}",
        "full_reply": full_reply,
        "time_to_first_audio": t_first_audio,
        "total_time": t_total,
        "word_count": word_count,
    }
