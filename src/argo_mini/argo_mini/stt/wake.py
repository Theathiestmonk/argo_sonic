"""
Wake word detection ? two backends:

  VoskWakeDetector  (default, recommended)
    Grammar-constrained Vosk recognizer ? only fires on exact wake phrases.
    Zero false positives, proven reliable for proper nouns like "sonic"/"argo".

  WakeWordDetector  (optional, future)
    Neural OpenWakeWord detector ? requires a custom-trained .onnx model.
    Use scripts/train_wake_word.py to train hey_sonic.onnx.
    Until trained, VoskWakeDetector is always the better choice.
"""

import json
import logging
import os
from typing import Optional

import numpy as np

logger = logging.getLogger("argo.stt.wake")


# ?? Vosk grammar wake detector ?????????????????????????????????????????????????

class VoskWakeDetector:
    """
    Grammar-constrained Vosk wake word detector.

    Only recognises the exact phrases in WAKE_GRAMMAR ? no false triggers
    from background speech.  Processes raw int16 audio in any chunk size.

    Args:
        model_path  : path to Vosk model directory
        sample_rate : audio sample rate (must match recording, default 16000)
    """

    WAKE_GRAMMAR = json.dumps([
        "sonic", "hey sonic", "hello sonic",
        "argo", "hey argo",
        "[unk]",
    ])

    def __init__(self, model_path: str, sample_rate: int = 16000):
        self._rec = None
        self._sr  = sample_rate

        try:
            from vosk import Model as VoskModel, KaldiRecognizer, SetLogLevel
            SetLogLevel(-1)  # suppress Vosk internal logs
            model     = VoskModel(model_path)
            self._rec = KaldiRecognizer(model, sample_rate)
            self._rec.SetGrammar(self.WAKE_GRAMMAR)
            logger.info(f"[WAKE] Vosk wake detector ready ? model: {os.path.basename(model_path)}")
        except ImportError:
            logger.error("[WAKE] vosk not installed ? pip3 install vosk")
        except Exception as e:
            logger.error(f"[WAKE] Vosk init failed: {e}")

    @property
    def available(self) -> bool:
        return self._rec is not None

    def process(self, chunk: np.ndarray) -> tuple[bool, str, float]:
        """
        Feed audio chunk.  Returns (detected, word, confidence).
        Only returns detected=True on a FINAL result (after brief silence).
        """
        if self._rec is None:
            return False, "", 0.0

        if self._rec.AcceptWaveform(chunk.tobytes()):
            text = json.loads(self._rec.Result()).get("text", "").strip().lower()
            if text and text != "[unk]":
                logger.info(f"[WAKE] Vosk detected: '{text}'")
                return True, text, 1.0

        return False, "", 0.0

    def reset_cooldown(self):
        """No-op ? Vosk grammar naturally ignores post-wake audio."""
        pass


# ?? OpenWakeWord neural detector (future / optional) ??????????????????????????

_COOLDOWN_CHUNKS = 15  # 15 � 80ms = 1.2s cooldown after detection


class WakeWordDetector:
    """
    Neural OpenWakeWord detector.

    Requires a custom-trained .onnx model (scripts/train_wake_word.py).
    Pre-trained models (hey_jarvis, hey_mycroft?) are NOT loaded ? they
    trigger on the robot's own TTS voice causing false wakes.

    Args:
        model_paths : list of paths to custom .onnx wake-word models
        threshold   : confidence threshold (0?1)
    """

    CHUNK_SAMPLES = 1280  # 80 ms at 16 kHz ? required by OpenWakeWord

    def __init__(
        self,
        model_paths: Optional[list] = None,
        model_names: Optional[list] = None,
        threshold: float = 0.5,
    ):
        self.threshold  = threshold
        self._cooldown  = 0
        self._model     = None

        if not model_paths:
            logger.info("[WAKE-OWW] No custom model paths provided ? OWW inactive.")
            return

        try:
            from openwakeword.model import Model
            existing = [p for p in model_paths if os.path.exists(p)]
            if not existing:
                logger.warning(f"[WAKE-OWW] None of the model paths exist: {model_paths}")
                return
            self._model = Model(wakeword_model_paths=existing, inference_framework="onnx")
            logger.info(f"[WAKE-OWW] Custom models loaded: {[os.path.basename(p) for p in existing]}")
        except ImportError:
            logger.warning("[WAKE-OWW] openwakeword not installed ? OWW inactive.")
        except Exception as e:
            logger.error(f"[WAKE-OWW] Init failed: {e}")

    @property
    def available(self) -> bool:
        return self._model is not None

    def process(self, chunk: np.ndarray) -> tuple[bool, str, float]:
        if self._model is None:
            return False, "", 0.0
        if self._cooldown > 0:
            self._cooldown -= 1
            return False, "", 0.0
        try:
            predictions = self._model.predict(chunk, debounce_time=0.0)
            best_word, best_score = "", 0.0
            for word, score in predictions.items():
                if score > best_score:
                    best_word, best_score = word, float(score)
            if best_score >= self.threshold:
                self._cooldown = _COOLDOWN_CHUNKS
                logger.info(f"[WAKE-OWW] '{best_word}' score={best_score:.2f}")
                return True, best_word, best_score
        except Exception as e:
            logger.debug(f"[WAKE-OWW] predict error: {e}")
        return False, "", 0.0

    def reset_cooldown(self):
        self._cooldown = 0
