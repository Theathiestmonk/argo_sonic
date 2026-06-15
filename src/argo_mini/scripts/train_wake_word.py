#!/usr/bin/env python3
"""
Train a custom OpenWakeWord model for "hey sonic" / "hey argo".

Uses Piper TTS to synthesize training data — no microphone recordings needed.
Training takes ~5 minutes on Jetson Orin Nano Super.

Usage:
    python3 scripts/train_wake_word.py --wake-word "hey sonic" --model-dir ~/argo_models

Output:
    ~/argo_models/hey_sonic.onnx  (use this in restaurant_agent_node.py)

Requirements:
    pip3 install openwakeword[training]
    /home/argo/piper/piper  (already installed)
"""

import argparse
import logging
import os
import subprocess
import tempfile
import sys

import numpy as np

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger("train_wake_word")


def generate_samples_piper(
    wake_word: str,
    output_dir: str,
    piper_binary: str,
    piper_model: str,
    count: int = 150,
) -> list:
    """Generate synthetic wake word audio using Piper TTS."""
    os.makedirs(output_dir, exist_ok=True)
    paths = []

    # Variations to make the model robust
    variations = [
        wake_word,
        wake_word + ".",
        wake_word + "!",
        "um " + wake_word,
        wake_word + " please",
    ]

    logger.info(f"Generating {count} Piper samples for '{wake_word}'...")
    for i in range(count):
        text = variations[i % len(variations)]
        out_path = os.path.join(output_dir, f"sample_{i:04d}.wav")

        try:
            proc = subprocess.Popen(
                [piper_binary, "--model", piper_model,
                 "--output_file", out_path],
                stdin=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
            proc.communicate(input=text.encode(), timeout=10)
            if os.path.exists(out_path):
                paths.append(out_path)
        except Exception as e:
            logger.warning(f"Sample {i} failed: {e}")

    logger.info(f"Generated {len(paths)} samples")
    return paths


def train(
    wake_word: str,
    model_dir: str,
    piper_binary: str,
    piper_model: str,
    n_samples: int = 150,
):
    try:
        import openwakeword
        from openwakeword.train import train as oww_train
    except ImportError:
        logger.error(
            "openwakeword[training] not installed.\n"
            "Run: pip3 install openwakeword[training]"
        )
        sys.exit(1)

    model_name = wake_word.lower().replace(" ", "_")
    output_model = os.path.join(model_dir, f"{model_name}.onnx")
    os.makedirs(model_dir, exist_ok=True)

    with tempfile.TemporaryDirectory() as tmp:
        pos_dir = os.path.join(tmp, "positive")
        logger.info("Step 1/3: Generating positive training samples with Piper...")
        pos_files = generate_samples_piper(
            wake_word, pos_dir, piper_binary, piper_model, n_samples
        )

        if len(pos_files) < 50:
            logger.error(f"Not enough samples ({len(pos_files)}). Check Piper setup.")
            sys.exit(1)

        logger.info("Step 2/3: Training OpenWakeWord model...")
        try:
            oww_train(
                positive_reference_clips=pos_files,
                output_dir=model_dir,
                model_name=model_name,
                n_epochs=100,
            )
        except TypeError:
            # API may differ by version — try alternative signature
            logger.info("Trying alternative training API...")
            from openwakeword.train import train_model
            train_model(
                positive_clips=pos_files,
                output_path=output_model,
            )

        logger.info(f"Step 3/3: Model saved → {output_model}")

    print("\n" + "=" * 50)
    print(f"Wake word model trained: {output_model}")
    print("\nUpdate restaurant_agent_node.py:")
    print(f'  OWW_MODEL_PATHS = ["{output_model}"]')
    print("=" * 50)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Train custom OpenWakeWord model")
    parser.add_argument("--wake-word",    default="hey sonic")
    parser.add_argument("--model-dir",    default=os.path.expanduser("~/argo_models"))
    parser.add_argument("--piper-binary", default="/home/argo/piper/piper")
    parser.add_argument("--piper-model",  default="/home/argo/piper-voices/en_US-lessac-medium.onnx")
    parser.add_argument("--n-samples",    type=int, default=150)
    args = parser.parse_args()

    train(
        wake_word=args.wake_word,
        model_dir=args.model_dir,
        piper_binary=args.piper_binary,
        piper_model=args.piper_model,
        n_samples=args.n_samples,
    )
