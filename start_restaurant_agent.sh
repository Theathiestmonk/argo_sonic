#!/bin/bash
# Argo Mini — Restaurant Agent
#
# Usage:
#   ./start_restaurant_agent.sh              # default voices + vosk model
#   ./start_restaurant_agent.sh --ryan       # use Ryan (male) voice
#   ./start_restaurant_agent.sh --oww        # enable OWW neural wake word (needs trained model)

source /opt/ros/humble/setup.bash
source ~/argo_mini_ws/install/setup.bash

# ── Paths ─────────────────────────────────────────────────────────────────────
VOSK_MODEL=~/dhruvil/argo_mini_ws/src/argo_mini/argo_mini/STT_project/vosk-model-small-en-us-0.15
PIPER_BIN=~/piper/piper
PIPER_MODEL_LESSAC=~/piper-voices/en_US-lessac-medium.onnx
PIPER_MODEL_RYAN=~/piper-voices/en_US-ryan-medium.onnx
OWW_MODEL=~/argo_mini_ws/src/argo_mini/argo_mini/stt/Hey_Tom_20260615_085211.onnx

# ── Defaults ──────────────────────────────────────────────────────────────────
PIPER_MODEL=$PIPER_MODEL_RYAN
USE_OWW=true

for arg in "$@"; do
  [[ "$arg" == "--ryan"   ]] && PIPER_MODEL=$PIPER_MODEL_RYAN
  [[ "$arg" == "--lessac" ]] && PIPER_MODEL=$PIPER_MODEL_LESSAC
  [[ "$arg" == "--oww"    ]] && USE_OWW=true
done

# Expand ~ in paths
VOSK_MODEL="${VOSK_MODEL/#\~/$HOME}"
PIPER_BIN="${PIPER_BIN/#\~/$HOME}"
PIPER_MODEL="${PIPER_MODEL/#\~/$HOME}"
OWW_MODEL="${OWW_MODEL/#\~/$HOME}"

# ── Checks ────────────────────────────────────────────────────────────────────
if [ ! -d "$VOSK_MODEL" ]; then
  echo "[agent] ERROR: Vosk model not found at $VOSK_MODEL"
  exit 1
fi
if [ ! -f "$PIPER_BIN" ]; then
  echo "[agent] ERROR: Piper binary not found at $PIPER_BIN"
  exit 1
fi
if [ ! -f "$PIPER_MODEL" ]; then
  echo "[agent] ERROR: Piper model not found at $PIPER_MODEL"
  exit 1
fi

# ── Build ROS args ────────────────────────────────────────────────────────────
ROS_ARGS=(
  --ros-args
  -p vosk_model_path:="$VOSK_MODEL"
  -p piper_binary:="$PIPER_BIN"
  -p piper_model:="$PIPER_MODEL"
  -p wake_timeout:=15.0
  -p sample_rate:=16000
)

if [ "$USE_OWW" = true ]; then
  if [ -f "$OWW_MODEL" ]; then
    ROS_ARGS+=(-p oww_model_paths:="$OWW_MODEL")
    echo "[agent] OWW neural wake word enabled: $OWW_MODEL"
  else
    echo "[agent] WARNING: OWW model not found at $OWW_MODEL — using Vosk wake"
  fi
fi

# ── Launch ────────────────────────────────────────────────────────────────────
echo ""
echo "========================================"
echo "  ARGO MINI — RESTAURANT AGENT"
echo "========================================"
echo "  Wake word : 'hey tom' (OWW) / 'sonic' (Vosk fallback)"
echo "  Listening : 15 seconds after wake"
echo "  Voice     : $(basename $PIPER_MODEL .onnx)"
echo "  Vosk      : $(basename $VOSK_MODEL)"
echo ""
echo "  Say 'sonic' to wake, then:"
echo "    'go to table three'"
echo "    'bring the bill'"
echo "    'call the waiter'"
echo "    'do a spin'"
echo "========================================"
echo ""

exec ros2 run argo_mini restaurant_agent "${ROS_ARGS[@]}"
