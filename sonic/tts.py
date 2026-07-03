"""
tts.py
------
Sarvam TTS wrapper using bulbul:v3 with the "shubh" speaker.
Sends text, gets back audio, plays it.
Used by both the fast-matcher lane and the Groq LLM lane.
"""

import base64
import io
import time
import requests
import config

try:
    import sounddevice as sd
    import soundfile as sf
    AUDIO_PLAYBACK_AVAILABLE = True
except ImportError:
    AUDIO_PLAYBACK_AVAILABLE = False
    print("[TTS] sounddevice/soundfile not installed — install with:")
    print("      pip install sounddevice soundfile")


def speak(text: str, block: bool = True) -> float:
    """
    Send text to Sarvam TTS, play the resulting audio.
    Returns the time (in seconds) taken for the full TTS call + playback start.
    block=True waits for audio to finish playing before returning (queues smoothly).
    block=False fires playback async (used in word-by-word mode to avoid
    blocking the next word's generation — but causes audio overlap, see notes).
    """
    if not text.strip():
        return 0.0

    t0 = time.time()
    try:
        response = requests.post(
            config.SARVAM_TTS_URL,
            headers={
                "api-subscription-key": config.SARVAM_API_KEY,
                "Content-Type": "application/json",
            },
            json={
                "inputs": [text],
                "target_language_code": config.SARVAM_LANGUAGE,
                "model": config.SARVAM_TTS_MODEL,        # bulbul:v3
                "speaker": config.SARVAM_SPEAKER,         # shubh
                "pace": config.SARVAM_PACE,               # v3 range: 0.5-2.0
                "temperature": config.SARVAM_TEMPERATURE, # v3 only, 0.01-1.0
                "speech_sample_rate": config.SARVAM_SAMPLE_RATE,
                # NOTE: pitch/loudness/enable_preprocessing are NOT sent —
                # they're bulbul:v2-only and are ignored (or error) on v3.
            },
            timeout=config.SARVAM_TIMEOUT,
        )
        response.raise_for_status()
        audio_b64 = response.json()["audios"][0]
        audio_bytes = base64.b64decode(audio_b64)

        if AUDIO_PLAYBACK_AVAILABLE:
            data, samplerate = sf.read(io.BytesIO(audio_bytes))
            sd.play(data, samplerate)
            if block:
                sd.wait()
        else:
            # fallback: just print, no audio hardware available
            print(f"  🔊 (no audio) Sonic would say: \"{text}\"")

        elapsed = time.time() - t0
        print(f"  🔊 [{elapsed:.2f}s] Sonic: {text}")
        return elapsed

    except requests.exceptions.ConnectionError:
        print(f"  [TTS] Connection error — is your internet working? Text was: {text}")
        return 0.0
    except requests.exceptions.Timeout:
        print(f"  [TTS] Timeout — Sarvam took too long. Text was: {text}")
        return 0.0
    except Exception as e:
        print(f"  [TTS] Error: {e} — Text was: {text}")
        return 0.0
