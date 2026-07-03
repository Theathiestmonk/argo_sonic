"""
llm_voice.py
------------
Sonic's production voice pipeline — SENTENCE-CHUNK streaming.

Chosen after A/B testing against word-by-word streaming: this mode
sounds natural (full sentence context for TTS prosody) while still
feeling fast, since each sentence speaks the moment it's ready instead
of waiting for the entire reply to generate.

Buffers tokens from Groq until a sentence boundary (. ! ?) is hit,
then sends that complete sentence to TTS immediately while the next
sentence keeps generating in the background.
"""

import json
import re
import time
import requests
import config
from tts import speak


# Scenario context injected into system prompt when a scenario is active
_SCENARIO_CONTEXT = {
    "restaurant": (
        "You are currently working in a RESTAURANT. You help guests with ordering, "
        "menus, bills, dietary questions, and dining experience. Stay focused on "
        "restaurant topics. For general chat that's fine, but never respond to "
        "hotel, bar, or home commands — those are out of scope."
    ),
    "hotel": (
        "You are currently working in a HOTEL. You help guests with check-in, "
        "check-out, room services, housekeeping, navigation around the hotel, "
        "and guest needs. Stay focused on hotel topics."
    ),
    "bar": (
        "You are currently working in a BAR. You help customers with drink orders, "
        "the drinks menu, recommendations, and bar service. Stay focused on bar topics."
    ),
    "home": (
        "You are currently working as a HOME assistant. You control smart devices "
        "(lights, fan, AC, TV), navigate between rooms, and manage home routines. "
        "Stay focused on home automation topics."
    ),
}


def build_system_prompt(active_scenario: str = None) -> str:
    """Build the full system prompt with scenario context injected."""
    if active_scenario and active_scenario in _SCENARIO_CONTEXT:
        scenario_context = (
            f"CURRENT SCENARIO: {_SCENARIO_CONTEXT[active_scenario]}\n\n"
            "Only answer questions relevant to your current scenario. For general "
            "robot questions (battery, identity, follow me, etc.) always answer normally."
        )
    else:
        scenario_context = ""

    return config.SONIC_BASE_PROMPT.format(scenario_context=scenario_context)


def run(user_text: str, conversation_history: list = None,
        active_scenario: str = None) -> dict:
    """
    Run one full turn: Groq streaming → sentence buffering → Sarvam TTS per sentence.
    active_scenario: injects the right context into the system prompt so the LLM
                     stays focused on the selected environment.
    Returns timing stats.
    """
    history = conversation_history or []
    messages = [{"role": "system", "content": build_system_prompt(active_scenario)}]
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
