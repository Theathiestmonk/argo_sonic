"""openWakeWord "hi sonic" detector, fed from the shared mic."""
from __future__ import annotations

import asyncio
import logging

import numpy as np

from .. import prefs

log = logging.getLogger("nlp.wake")


class WakeWord:
    PATIENCE = 2        # 80 ms frames over threshold needed...
    WINDOW = 4          # ...within this many recent frames (one weak frame no longer resets the count)
    MIN_RMS = 60        # int16 RMS floor (was 150 – above a normal quiet-mic "hi sonic"; idle room noise is ~20-30)
    BARGE_BOOST = 0.3   # extra threshold mid-session (the robot's own "Sonic" and the user's speech leak in)
    BARGE_PATIENCE = 3  # frames needed mid-session

    def __init__(self, model_path: str, threshold: float):
        from openwakeword.model import Model
        self._default_threshold = threshold
        self._hits = 0
        self._peak = 0.0
        self._recent: list[bool] = []
        self.model = Model(wakeword_models=[model_path], inference_framework="onnx")
        self.model.predict(np.zeros(1280, dtype=np.int16))      # first call is slow (graph init) – pay it now
        log.info("wake model loaded: %s", model_path)

    @property
    def threshold(self) -> float:
        """Live-tunable from the UI (backend/voice_prefs.json)."""
        try:
            return float(prefs.get("wake_threshold", self._default_threshold))
        except (TypeError, ValueError):
            return self._default_threshold

    def score(self, frame: bytes) -> float:
        scores = self.model.predict(np.frombuffer(frame, dtype=np.int16))
        return max(scores.values()) if scores else 0.0

    def reset(self) -> None:
        self.model.reset()
        self._hits = 0
        self._peak = 0.0
        self._recent = []

    def detect(self, frame: bytes, boost: float = 0.0) -> float:
        """Feed one 80 ms frame. Returns the score when the wake word is confirmed, else 0.0.
        Confirmation = `patience` consecutive frames over (threshold + boost) AND the audio is not near-silent.
        `boost` raises the bar while the robot itself is talking (barge-in)."""
        pcm = np.frombuffer(frame, dtype=np.int16)
        sc = self.score(frame)
        rms = float(np.sqrt(np.mean(pcm.astype(np.float32) ** 2))) if pcm.size else 0.0
        thr = self.threshold + boost
        need = self.BARGE_PATIENCE if boost else self.PATIENCE
        hit = sc >= thr and rms >= self.MIN_RMS
        if sc >= 0.3:                                   # near-miss log: shows why a real "hi sonic" was rejected
            log.info("wake score %.2f (thr %.2f, rms %.0f, hit %s)", sc, thr, rms, hit)
        self._recent = (self._recent + [hit])[-self.WINDOW:]
        if sum(self._recent) >= need:
            log.info("wake confirmed: score %.2f rms %.0f", sc, rms)
            self.reset()
            return sc
        return 0.0

    async def wait(self, mic_queue: asyncio.Queue) -> float:
        self.reset()
        while True:
            sc = self.detect(await mic_queue.get())
            if sc:
                return sc
