"""
wake_word.py
------------
Continuous wake word detection using OpenWakeWord and the custom
Hi_Sonic.onnx model.

Listens to the microphone in small chunks and runs the model on each
chunk. When the detection score crosses the threshold, the wake word
is considered "heard" and the main loop moves on to recording the
actual command.

Install:
    pip install openwakeword sounddevice numpy
"""

import time
import numpy as np
import config

try:
    import sounddevice as sd
    from openwakeword.model import Model
    WAKE_WORD_DEPS_AVAILABLE = True
except ImportError as e:
    WAKE_WORD_DEPS_AVAILABLE = False
    _import_error = e


def check_dependencies() -> bool:
    """Check that openwakeword + sounddevice are installed."""
    if not WAKE_WORD_DEPS_AVAILABLE:
        print(f"  [WakeWord] Missing dependency: {_import_error}")
        print("  [WakeWord] Install with: pip install openwakeword sounddevice")
        return False
    return True


def load_model():
    """
    Load the Hi_Sonic.onnx wake word model.
    Returns the Model object, or None if loading fails.
    """
    if not check_dependencies():
        return None

    import os
    if not os.path.exists(config.WAKE_WORD_MODEL_PATH):
        print(f"  [WakeWord] Model file not found: {config.WAKE_WORD_MODEL_PATH}")
        print(f"  [WakeWord] Place Hi_Sonic.onnx in the models/ folder.")
        return None

    try:
        model = Model(
            wakeword_models=[config.WAKE_WORD_MODEL_PATH],
            inference_framework="onnx",
        )
        print(f"  [WakeWord] ✅ Loaded model: {config.WAKE_WORD_MODEL_PATH}")
        return model
    except Exception as e:
        print(f"  [WakeWord] Failed to load model: {e}")
        return None


def listen_for_wake_word(model, threshold: float = None) -> bool:
    """
    Block until the wake word is detected from the live microphone.
    Returns True once detected. Prints a heartbeat dot periodically
    so you know it's alive and listening, not frozen.
    """
    threshold = threshold or config.WAKE_WORD_THRESHOLD
    sample_rate = 16000
    chunk_size = 1280  # openwakeword expects 80ms chunks at 16kHz

    print(f"\n  👂 Listening for wake word (\"Hey Sonic\")...")

    last_heartbeat = time.time()

    with sd.InputStream(
        samplerate=sample_rate,
        channels=1,
        dtype="int16",
        blocksize=chunk_size,
    ) as stream:
        while True:
            audio_chunk, _ = stream.read(chunk_size)
            audio_chunk = audio_chunk.flatten().astype(np.int16)

            prediction = model.predict(audio_chunk)

            # prediction is a dict: {model_name: score}
            for model_name, score in prediction.items():
                if score > threshold:
                    print(f"\n  🎯 Wake word detected! (score: {score:.2f})")
                    model.reset()  # clear internal buffers for next detection
                    return True

            # heartbeat so the terminal doesn't look frozen
            if time.time() - last_heartbeat > 2.0:
                print(".", end="", flush=True)
                last_heartbeat = time.time()
