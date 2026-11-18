"""openWakeWord "hi sonic" detector, fed from the shared mic."""
from __future__ import annotations

import asyncio
import logging

import numpy as np

log = logging.getLogger("nlp.wake")


class WakeWord:
    def __init__(self, model_path: str, threshold: float):
        from openwakeword.model import Model
        self.threshold = threshold
        self.model = Model(wakeword_models=[model_path], inference_framework="onnx")
        self.model.predict(np.zeros(1280, dtype=np.int16))      # first call is slow (graph init) – pay it now
        log.info("wake model loaded: %s", model_path)

    def reset(self) -> None:
        self.model.reset()

    def score(self, frame: bytes) -> float:
        scores = self.model.predict(np.frombuffer(frame, dtype=np.int16))
        return max(scores.values()) if scores else 0.0

    async def wait(self, mic_queue: asyncio.Queue) -> float:
        self.model.reset()
        while True:
            best = self.score(await mic_queue.get())
            if best >= self.threshold:
                self.model.reset()
                return best
