#!/bin/bash
# Download Sherpa-ONNX streaming model + OpenWakeWord pre-trained models
# Run once on Jetson: bash scripts/download_models.sh

set -e
MODEL_DIR="${1:-$HOME/argo_models}"
mkdir -p "$MODEL_DIR"

echo "=========================================="
echo " Argo Mini STT Model Downloader"
echo " Target: $MODEL_DIR"
echo "=========================================="

# ?? Sherpa-ONNX streaming Zipformer (English) ?????????????????????????????????
SHERPA_MODEL="sherpa-onnx-streaming-zipformer-en-2023-06-26"
SHERPA_URL="https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/${SHERPA_MODEL}.tar.bz2"

if [ ! -d "$MODEL_DIR/$SHERPA_MODEL" ]; then
    echo ""
    echo "[1/3] Downloading Sherpa-ONNX Zipformer model (~65MB)..."
    wget -q --show-progress -O /tmp/sherpa_model.tar.bz2 "$SHERPA_URL"
    tar -xjf /tmp/sherpa_model.tar.bz2 -C "$MODEL_DIR"
    rm /tmp/sherpa_model.tar.bz2
    echo "    ? Sherpa-ONNX model ready: $MODEL_DIR/$SHERPA_MODEL"
else
    echo "[1/3] Sherpa-ONNX model already exists ? skipping"
fi

# ?? Hotwords file ?????????????????????????????????????????????????????????????
echo ""
echo "[2/3] Writing hotwords.txt..."
cat > "$MODEL_DIR/hotwords.txt" << 'EOF'
sonic
argo
go to table
return to base
bring the bill
call the waiter
ready to order
bring water
EOF
echo "    ? Hotwords written: $MODEL_DIR/hotwords.txt"

# ?? OpenWakeWord pre-trained models ???????????????????????????????????????????
echo ""
echo "[3/3] Downloading OpenWakeWord pre-trained models..."
python3 - << 'PYEOF'
try:
    from openwakeword.utils import download_models
    download_models()
    print("    ? OpenWakeWord models ready")
except ImportError:
    print("    ? openwakeword not installed ? run: pip3 install openwakeword")
except Exception as e:
    print(f"    ? Download failed: {e}")
PYEOF

# ?? Summary ???????????????????????????????????????????????????????????????????
echo ""
echo "=========================================="
echo " Done! Set these paths in restaurant_agent_node.py:"
echo ""
echo "   SHERPA_MODEL_DIR = \"$MODEL_DIR/$SHERPA_MODEL\""
echo "   HOTWORDS_FILE    = \"$MODEL_DIR/hotwords.txt\""
echo ""
echo " To train a custom 'hey sonic' wake word:"
echo "   python3 scripts/train_wake_word.py --model-dir $MODEL_DIR"
echo "=========================================="
