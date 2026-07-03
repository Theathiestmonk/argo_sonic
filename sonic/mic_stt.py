"""
mic_stt.py
----------
Records live speech from the microphone with two distinct recording modes:

  listen_and_transcribe()
      Called right after wake word fires — user is expected to speak
      immediately. Hard timeout of MIC_MAX_DURATION, stops 1.5s after
      silence. If nobody speaks within MIC_WAKE_WINDOW seconds (no
      volume above threshold), returns "" early so the loop can decide
      to go back to wake-word mode without waiting the full 12 seconds.

  listen_continued()
      Called for follow-up turns WITHIN an active conversation. Gives
      the user MIC_FOLLOW_UP_WINDOW seconds to start talking before
      timing out. Returns "" on timeout so main.py knows to go back to
      idle wake-word mode.

Transcription: Sarvam saarika:v2.5 REST API.
"""

import io
import time
import wave
import requests
import numpy as np
import config

try:
    import sounddevice as sd
    MIC_AVAILABLE = True
except ImportError:
    MIC_AVAILABLE = False


def check_dependencies() -> bool:
    if not MIC_AVAILABLE:
        print("  [STT] sounddevice not installed — run: pip install sounddevice")
        return False
    return True


# ── Core recorder ─────────────────────────────────────────────────────────────

def _record(
    sample_rate: int = 16000,
    silence_threshold: float = None,
    silence_duration: float = 1.5,
    max_duration: float = 12.0,
    start_window: float = None,
) -> tuple[bytes, bool]:
    """
    Internal recorder. Returns (wav_bytes, speech_detected).

    start_window: if set, bail out early with (b"", False) if no speech
                  is detected within this many seconds. This is the key
                  mechanism for the 5-second follow-up timeout.
    """
    threshold = silence_threshold or config.MIC_SILENCE_THRESHOLD
    chunk_duration = 0.1          # 100ms chunks
    chunk_samples = int(sample_rate * chunk_duration)
    silence_chunks_needed = int(silence_duration / chunk_duration)
    max_chunks = int(max_duration / chunk_duration)
    start_window_chunks = int(start_window / chunk_duration) if start_window else None

    frames = []
    silent_chunk_count = 0
    has_spoken = False
    chunks_elapsed = 0

    with sd.InputStream(
        samplerate=sample_rate,
        channels=1,
        dtype="int16",
        blocksize=chunk_samples,
    ) as stream:
        for _ in range(max_chunks):
            chunk, _ = stream.read(chunk_samples)
            chunk = chunk.flatten()
            frames.append(chunk)
            chunks_elapsed += 1

            volume = np.abs(chunk).mean()

            if volume > threshold:
                silent_chunk_count = 0
                has_spoken = True
            else:
                silent_chunk_count += 1

            # if user started talking, stop on silence as normal
            if has_spoken and silent_chunk_count >= silence_chunks_needed:
                break

            # if nobody started talking within the start window → bail early
            if start_window_chunks and not has_spoken:
                if chunks_elapsed >= start_window_chunks:
                    return b"", False

    if not has_spoken:
        return b"", False

    audio_data = np.concatenate(frames).astype(np.int16)
    return _to_wav(audio_data, sample_rate), True


def _to_wav(pcm: np.ndarray, sample_rate: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm.tobytes())
    return buf.getvalue()


# ── Public API ────────────────────────────────────────────────────────────────

def listen_and_transcribe() -> str:
    """
    Called immediately after wake word fires.
    User is expected to speak right away.
    If nothing is heard within MIC_WAKE_WINDOW seconds → return "" early.
    """
    if not check_dependencies():
        return ""

    print(f"  🎤 Listening... (speak now)")

    wav_bytes, spoke = _record(
        silence_threshold=config.MIC_SILENCE_THRESHOLD,
        silence_duration=config.MIC_SILENCE_DURATION,
        max_duration=config.MIC_MAX_DURATION,
        start_window=config.MIC_WAKE_WINDOW,
    )

    if not spoke:
        print("  🎤 No speech detected.")
        return ""

    print("  🎤 Got it, transcribing...")
    return _transcribe(wav_bytes)


def listen_continued(window: float = None) -> str:
    """
    Called for follow-up turns within an ongoing conversation.
    Gives the user `window` seconds to start talking before timing out.
    On timeout → returns "" so main.py goes back to wake-word mode.
    """
    if not check_dependencies():
        return ""

    follow_window = window or config.MIC_FOLLOW_UP_WINDOW
    print(f"  🎤 Listening for follow-up... ({follow_window:.0f}s window)")

    wav_bytes, spoke = _record(
        silence_threshold=config.MIC_SILENCE_THRESHOLD,
        silence_duration=config.MIC_SILENCE_DURATION,
        max_duration=config.MIC_MAX_DURATION,
        start_window=follow_window,
    )

    if not spoke:
        print("  🎤 No follow-up detected — returning to wake word mode.")
        return ""

    print("  🎤 Got it, transcribing...")
    return _transcribe(wav_bytes)


# ── Sarvam STT ────────────────────────────────────────────────────────────────

def _transcribe(wav_bytes: bytes) -> str:
    if not wav_bytes:
        return ""
    try:
        response = requests.post(
            config.SARVAM_STT_URL,
            headers={"api-subscription-key": config.SARVAM_API_KEY},
            files={"file": ("audio.wav", wav_bytes, "audio/wav")},
            data={
                "model": config.SARVAM_STT_MODEL,
                "language_code": config.SARVAM_LANGUAGE,
                "with_timestamps": "false",
                "with_disfluencies": "false",
            },
            timeout=config.SARVAM_TIMEOUT,
        )
        response.raise_for_status()
        transcript = response.json().get("transcript", "").strip()
        if transcript:
            print(f"  📝 Heard: \"{transcript}\"")
        else:
            print("  📝 Nothing in the audio.")
        return transcript

    except requests.exceptions.ConnectionError:
        print("  [STT] Connection error — check internet/DNS.")
        return ""
    except requests.exceptions.Timeout:
        print("  [STT] Sarvam STT timed out.")
        return ""
    except Exception as e:
        print(f"  [STT] Error: {e}")
        return ""
