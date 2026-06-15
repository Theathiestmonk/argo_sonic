"""
STT Pipeline ? Vosk for both wake detection and command recognition.

Vosk with grammar outperforms unconstrained streaming models (Sherpa, Whisper)
for known-vocabulary domains like restaurant control ? zero hallucinations,
works in noisy environments, sub-200ms latency.

State machine:
  SLEEPING  ? VoskWakeDetector (grammar: "sonic", "argo", "hey argo")
            ? on detection ? LISTENING
  LISTENING ? VoskCommandRecognizer (full restaurant grammar, ~60 phrases)
            ? on final result ? returns text ? SLEEPING
"""

import json
import logging
from typing import Callable, Optional

import numpy as np

from .wake import VoskWakeDetector, WakeWordDetector

logger = logging.getLogger("argo.stt.pipeline")

# Full restaurant command grammar ? covers all intents the agent handles
_COMMAND_GRAMMAR = json.dumps([
    # Navigation
    "go to table one", "go to table two", "go to table three",
    "go to table four", "go to table five", "go to table six",
    "go to table seven", "go to table eight", "go to table nine",
    "go to table ten", "go to table eleven", "go to table twelve",
    "table one", "table two", "table three", "table four", "table five",
    "table six", "table seven", "table eight", "table nine",
    "table ten", "table eleven", "table twelve",
    "return to base", "go home", "dock", "go back",
    # Waiter / service
    "call the waiter", "call a waiter", "need assistance",
    "bring the bill", "bill please", "check please",
    "bring water", "water please", "more water",
    "bring the menu", "menu please",
    "ready to order", "take my order", "we are ready to order",
    "take our order", "i am ready to order",
    "clear the table", "table is ready",
    # Ordering flow responses
    "yes", "no", "correct", "that is correct", "confirm", "cancel",
    "add more", "remove that", "change that",
    # General / chat
    "tell me a joke", "how are you", "who are you", "what can you do",
    "good morning", "good afternoon", "good evening",
    "thank you", "thanks", "great job", "well done",
    "hello", "hi there", "hey there",
    # Emergency / stop
    "stop", "emergency", "help",
    "[unk]",
])


class VoskCommandRecognizer:
    """
    Grammar-constrained Vosk recognizer for restaurant commands.

    Uses the same Vosk model as the wake detector but with the full
    restaurant command grammar.  Returns a final result after each
    complete utterance (silence following speech).
    """

    def __init__(self, model_path: str, sample_rate: int = 16000):
        self._model     = None
        self._rec       = None
        self._sr        = sample_rate
        self._KaldiRec  = None

        try:
            from vosk import Model as VoskModel, KaldiRecognizer, SetLogLevel
            SetLogLevel(-1)
            self._KaldiRec = KaldiRecognizer
            self._model    = VoskModel(model_path)
            self._rec      = self._make_rec()
            logger.info("[STT] Vosk command recognizer ready")
        except ImportError:
            logger.error("[STT] vosk not installed")
        except Exception as e:
            logger.error(f"[STT] Vosk command recognizer init failed: {e}")

    def _make_rec(self):
        """Always create a fresh KaldiRecognizer ? SetGrammar is init-only."""
        rec = self._KaldiRec(self._model, self._sr)
        rec.SetGrammar(_COMMAND_GRAMMAR)
        return rec

    @property
    def available(self) -> bool:
        return self._rec is not None

    def process(self, chunk: np.ndarray) -> tuple[str, bool, str]:
        """
        Feed audio chunk.
        Returns (partial, is_final, final_text).
        """
        if self._rec is None:
            return "", False, ""

        if self._rec.AcceptWaveform(chunk.tobytes()):
            text = json.loads(self._rec.Result()).get("text", "").strip().lower()
            if text and text != "[unk]":
                return "", True, text
            return "", True, ""   # silence / unk ? empty final

        return "", False, ""

    def reset(self):
        """Create a brand-new KaldiRecognizer ? the only safe way to reset."""
        if self._model is not None:
            self._rec = self._make_rec()


class STTPipeline:
    """
    Two-stage Vosk pipeline:
      1. VoskWakeDetector       ? "sonic" / "argo" / "hey argo"
      2. VoskCommandRecognizer  ? full restaurant grammar

    Optionally, a WakeWordDetector (OWW) can be passed for neural wake
    detection once a custom model is trained (scripts/train_wake_word.py).

    Usage:
        pipeline = STTPipeline(vosk_model_path)

        # While sleeping:
        woke, word = pipeline.process_sleeping(chunk)

        # While listening:
        final_text = pipeline.process_listening(chunk)  # "" until utterance ends
    """

    def __init__(
        self,
        wake: VoskWakeDetector,
        recognizer,                          # kept for API compat (unused)
        oww: Optional[WakeWordDetector] = None,
        on_partial: Optional[Callable[[str], None]] = None,
        vosk_model_path: str = "",
        sample_rate: int = 16000,
    ):
        self.wake    = wake
        self._oww    = oww
        self._cmd    = VoskCommandRecognizer(vosk_model_path or "", sample_rate)

        if not wake.available:
            logger.warning("[STT] Vosk wake detector not available")
        if not self._cmd.available:
            logger.warning("[STT] Vosk command recognizer not available")
        else:
            logger.info("[STT] Pipeline: Vosk wake + Vosk commands")

    def process_sleeping(self, chunk: np.ndarray) -> tuple[bool, str]:
        """Returns (woke, wake_word)."""
        detected, word, _ = self.wake.process(chunk)
        if detected:
            self._cmd.reset()
            return True, word

        if self._oww and self._oww.available:
            detected, word, _ = self._oww.process(chunk)
            if detected:
                self._cmd.reset()
                return True, word

        return False, ""

    def process_listening(self, chunk: np.ndarray) -> str:
        """Returns final command text when utterance ends, "" otherwise."""
        _, is_final, text = self._cmd.process(chunk)
        if is_final and text:
            logger.info(f"[STT] Final: '{text}'")
            return text
        return ""

    def flush(self):
        """Reset after TTS playback."""
        self.wake.reset_cooldown()
        self._cmd.reset()
