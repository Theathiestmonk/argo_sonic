"""
Sherpa-ONNX streaming ASR ? production-grade, GPU-accelerated.

Why Sherpa-ONNX over Whisper / Vosk:
  - Streaming: returns partial results word-by-word as you speak
  - Endpoint detection: knows when you've finished speaking (no silence hacks)
  - Full vocabulary: no grammar constraints, recognizes any phrase naturally
  - GPU: ONNX Runtime CUDA EP on Jetson ? ~50-100ms latency
  - Hotword boosting: boosts key phrases ("go to table", "sonic") in decoding

Model: sherpa-onnx-streaming-zipformer-en-2023-06-26
  Encoder: Zipformer (best accuracy/speed tradeoff)
  Decoder: RNNT transducer (streaming-native)
  Download: scripts/download_models.sh
"""

import logging
import os
from typing import Optional

import numpy as np

logger = logging.getLogger("argo.stt.recognizer")

_SAMPLE_RATE = 16000


class StreamingRecognizer:
    """
    Real-time streaming ASR using Sherpa-ONNX transducer.

    Args:
        model_dir        : directory containing encoder/decoder/joiner .onnx + tokens.txt
        provider         : "cuda" (Jetson GPU) or "cpu"
        rule1_silence    : trailing silence (s) for long pauses ? endpoint
        rule2_silence    : trailing silence (s) for quick commands ? endpoint
        hotwords_file    : path to hotwords.txt for score boosting
        hotwords_score   : boost factor for hotwords (10?20 recommended)
    """

    def __init__(
        self,
        model_dir: str,
        provider: str = "cuda",
        num_threads: int = 2,
        rule1_silence: float = 2.4,
        rule2_silence: float = 1.2,
        hotwords_file: str = "",
        hotwords_score: float = 15.0,
    ):
        self._sr = _SAMPLE_RATE
        self._recognizer = None

        import glob as _glob

        def _find(prefix: str) -> str:
            # Try exact names first, then glob for any variant (e.g. -chunk-16-left-128)
            # Prefer int8 (smaller, faster on Jetson CPU) over full-precision
            for pattern in [
                os.path.join(model_dir, f"{prefix}.int8.onnx"),
                os.path.join(model_dir, f"{prefix}.onnx"),
                os.path.join(model_dir, f"{prefix}*.int8.onnx"),
                os.path.join(model_dir, f"{prefix}*.onnx"),
            ]:
                matches = _glob.glob(pattern)
                if matches:
                    return sorted(matches)[0]
            raise FileNotFoundError(
                f"No {prefix}*.onnx found in {model_dir}.\n"
                "Run scripts/download_models.sh to download the model."
            )

        encoder = _find("encoder-epoch-99-avg-1")
        decoder = _find("decoder-epoch-99-avg-1")
        joiner  = _find("joiner-epoch-99-avg-1")
        tokens  = os.path.join(model_dir, "tokens.txt")

        if not os.path.exists(tokens):
            raise FileNotFoundError(f"tokens.txt not found in {model_dir}")

        hw_file = hotwords_file if os.path.exists(hotwords_file) else ""
        if hw_file:
            logger.info(f"[STT] Hotwords file: {hw_file}")

        # Try CUDA ? fall back to CPU automatically
        providers = ["cuda", "cpu"] if provider == "cuda" else ["cpu"]
        for prov in providers:
            try:
                import sherpa_onnx
                self._recognizer = sherpa_onnx.OnlineRecognizer.from_transducer(
                    encoder=encoder,
                    decoder=decoder,
                    joiner=joiner,
                    tokens=tokens,
                    num_threads=num_threads,
                    provider=prov,
                    sample_rate=self._sr,
                    feature_dim=80,
                    enable_endpoint_detection=True,
                    rule1_min_trailing_silence=rule1_silence,
                    rule2_min_trailing_silence=rule2_silence,
                    rule3_min_utterance_length=20.0,
                    decoding_method="modified_beam_search",
                    hotwords_file=hw_file,
                    hotwords_score=hotwords_score,
                )
                logger.info(f"[STT] Sherpa-ONNX ready  encoder={os.path.basename(encoder)}  provider={prov}")
                break
            except Exception as e:
                logger.warning(f"[STT] Provider '{prov}' failed: {e}")

        if self._recognizer is None:
            raise RuntimeError("[STT] Could not initialize Sherpa-ONNX recognizer")

        self._stream = self._recognizer.create_stream()

    # ?? Public API ?????????????????????????????????????????????????????????????

    def process(self, chunk: np.ndarray) -> tuple[str, bool, str]:
        """
        Feed one audio chunk and decode.

        Args:
            chunk: int16 numpy array at 16 kHz

        Returns:
            (partial, is_endpoint, final)
            - partial     : live partial text (for UI display)
            - is_endpoint : True when Sherpa detects end of utterance
            - final       : non-empty string when is_endpoint=True
        """
        samples = chunk.astype(np.float32) / 32768.0
        self._stream.accept_waveform(self._sr, samples)

        while self._recognizer.is_ready(self._stream):
            self._recognizer.decode_streams([self._stream])

        _res = self._recognizer.get_result(self._stream)
        partial = (_res if isinstance(_res, str) else _res.text).strip().lower()
        is_endpoint = self._recognizer.is_endpoint(self._stream)

        final = ""
        if is_endpoint:
            final = partial
            self._new_stream()

        return partial, is_endpoint, final

    def reset(self):
        """Force-reset for use after TTS or state change."""
        self._new_stream()

    # ?? Private ???????????????????????????????????????????????????????????????

    def _new_stream(self):
        self._stream = self._recognizer.create_stream()
