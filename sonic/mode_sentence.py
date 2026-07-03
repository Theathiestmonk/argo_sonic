"""
mode_sentence.py
-----------------
SENTENCE-CHUNK streaming mode.

Buffers tokens from Groq until a sentence boundary (. ! ?) is hit,
then sends that complete sentence to TTS immediately while the next
sentence keeps generating in the background.

This is the recommended mode — natural prosody, near-instant perceived
response time, no choppy audio.
"""

import json
import re
import time
import requests
import config
from tts import speak


def run(user_text: str, conversation_history: list = None) -> dict:
    """
    Run one full turn: Groq streaming → sentence buffering → Sarvam TTS per sentence.
    Returns timing stats for comparison against word-by-word mode.
    """
    history = conversation_history or []
    messages = [{"role": "system", "content": config.SONIC_SYSTEM_PROMPT}]
    messages += history
    messages.append({"role": "user", "content": user_text})

    print(f"\n  You: {user_text}")
    t_start = time.time()
    t_first_audio = None
    sentence_count = 0

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
    sentence_end_pattern = re.compile(r'([.!?])\s')

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

        match = sentence_end_pattern.search(buffer)
        if match:
            sentence = buffer[:match.end()].strip()
            buffer = buffer[match.end():]

            if t_first_audio is None:
                t_first_audio = time.time() - t_start
                print(f"  ⏱️  Time to first audio: {t_first_audio:.2f}s")

            speak(sentence, block=True)
            sentence_count += 1

    # speak whatever's left in the buffer (no trailing punctuation)
    if buffer.strip():
        if t_first_audio is None:
            t_first_audio = time.time() - t_start
        speak(buffer.strip(), block=True)
        sentence_count += 1

    t_total = time.time() - t_start

    return {
        "mode": "sentence_chunk",
        "full_reply": full_reply,
        "time_to_first_audio": t_first_audio,
        "total_time": t_total,
        "sentence_count": sentence_count,
    }
