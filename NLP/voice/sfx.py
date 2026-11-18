"""Sound effects. The 'I heard nothing' cue is the safety shield's own alert beep (sound/beep.mp3,
same file, same 0.85 gain as safety_shield.ALERT_VOLUME), decoded once at start-up so it plays instantly."""
from __future__ import annotations

import subprocess
from pathlib import Path

import numpy as np

from ..config import REPO_ROOT

BEEP_FILE = REPO_ROOT / "sound" / "beep.mp3"
ALERT_VOLUME = 0.85


def shield_beep(rate: int = 22050, path: Path = BEEP_FILE, volume: float = ALERT_VOLUME) -> bytes:
    """Decode the mp3 to mono PCM16 at `rate` (needs ffmpeg). Returns b'' if unavailable."""
    try:
        raw = subprocess.run(
            ["ffmpeg", "-v", "error", "-i", str(path), "-f", "s16le", "-ac", "1", "-ar", str(rate), "-"],
            capture_output=True, check=True, timeout=10).stdout
    except Exception:
        return b""
    return (np.frombuffer(raw, dtype=np.int16).astype(np.float32) * volume).astype(np.int16).tobytes()
