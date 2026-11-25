"""Live voice preferences written by the UI (backend/voice_prefs.json).
Read with a small mtime cache so the audio callbacks can poll it cheaply.
Keys: mic_device, speaker_device (applied at agent start), mic_muted, speaker_muted, wake_threshold (live)."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

PATH = Path(os.getenv("NLP_VOICE_PREFS", str(Path(__file__).resolve().parent.parent / "backend" / "voice_prefs.json")))
_cache: dict = {}
_checked = 0.0
_mtime = None


def load() -> dict:
    global _cache, _checked, _mtime
    now = time.monotonic()
    if now - _checked < 0.4:
        return _cache
    _checked = now
    try:
        m = PATH.stat().st_mtime
        if m != _mtime:
            d = json.loads(PATH.read_text())
            _cache, _mtime = (d if isinstance(d, dict) else {}), m
    except (OSError, ValueError):
        _cache, _mtime = {}, None
    return _cache


def get(key: str, default=None):
    v = load().get(key)
    return default if v is None or v == "" else v
