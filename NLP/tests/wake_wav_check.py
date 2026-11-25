"""Offline wake-word test – no mic/speaker needed.
  python -m NLP.tests.wake_wav_check hi_sonic.wav [other.wav ...]   # any wav; resampled to 16 kHz mono
Each file is replayed at several volumes (x0.25 … x2) with room noise added, through the real WakeWord.
Use clips of "hi sonic" (should trigger) and of other speech / robot TTS (should NOT trigger)."""
import sys, wave
import numpy as np
from ..config import Settings
from ..voice.wake import WakeWord

def load(path):
    with wave.open(path) as w:
        raw = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32)
        if w.getnchannels() > 1: raw = raw.reshape(-1, w.getnchannels()).mean(1)
        sr = w.getframerate()
    if sr != 16000: raw = np.interp(np.arange(0, len(raw), sr / 16000), np.arange(len(raw)), raw)
    return raw

def run(w, x, boost=0.0):
    x = np.concatenate([np.zeros(16000), x, np.zeros(16000)]).clip(-32768, 32767).astype(np.int16)
    w.reset()
    for i in range(0, len(x) - 1279, 1280):
        sc = w.detect(x[i:i + 1280].tobytes(), boost)
        if sc: return sc
    return 0.0

if __name__ == "__main__":
    s = Settings(); w = WakeWord(s.wake_model, s.wake_threshold)
    rng = np.random.default_rng(0)
    for p in sys.argv[1:]:
        x = load(p)
        peak = float(np.abs(x).max()) or 1.0
        x = x / peak * 12000                                    # normalise to a typical speech level
        for g in (0.25, 0.5, 1.0, 2.0):
            y = x * g + rng.normal(0, 25, x.size)
            print(f"{p}  vol x{g:<4}  idle: {'TRIGGER' if run(w, y) else '-':8} mid-session: {'TRIGGER' if run(w, y, w.BARGE_BOOST) else '-'}")
