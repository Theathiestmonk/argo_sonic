#!/bin/bash
# Argo Mini — build the ceiling landmark map
#
# Usage:
#   sh/start_ceiling_map.sh                      # default map path
#   sh/start_ceiling_map.sh ~/maps/my.json       # custom
#   sh/start_ceiling_map.sh --extend             # add to an existing map
#
# Run the nav stack first (sh/start_argo_nav.sh --map ...): the builder needs
# map -> base_link from it, and the camera it starts.
#
# Ctrl-C when done. The map saves on the way out and every 20 s before that.

# (no `set -u`: the ROS setup scripts reference unbound variables by design)

MAP_PATH="$HOME/maps/atsn_cafe.ceiling.json"
AZIMUTH="92.361"
EXTEND="false"

for arg in "$@"; do
  case "$arg" in
    --extend) EXTEND="true" ;;
    --az=*)   AZIMUTH="${arg#--az=}" ;;
    -*)       echo "[ceiling] unknown option: $arg"; exit 1 ;;
    *)        MAP_PATH="${arg/#\~/$HOME}" ;;
  esac
done

source /opt/ros/humble/setup.bash
source "$HOME/my_project/argo_sonic/install/setup.bash"

# ── prerequisites ──────────────────────────────────────────────────────────
# Each of these has cost a drive at least once, so check rather than assume.
fail=0
if ! pgrep -f slam_toolbox >/dev/null; then
  echo "[ceiling] ✗ nav stack is not running — the map would not be registered"
  echo "[ceiling]   start it first:  sh/start_argo_nav.sh --map ~/maps/<your_map>"
  fail=1
fi
if ! pgrep -f ascamera_node >/dev/null; then
  echo "[ceiling] ✗ camera is not running"
  fail=1
fi
mkdir -p "$(dirname "$MAP_PATH")" 2>/dev/null
if ! touch "$MAP_PATH.probe" 2>/dev/null; then
  echo "[ceiling] ✗ cannot write to $MAP_PATH"
  fail=1
else
  rm -f "$MAP_PATH.probe"
fi
[ "$fail" = 1 ] && exit 1

# ── one detector, exactly ──────────────────────────────────────────────────
# Two instances publish to the same topic, so the builder interleaves readings
# from two independently-smoothed ceiling planes. It looks like sensor noise and
# it is not. This has happened; always start from a clean slate.
existing=$(pgrep -cf "argo_mini/ceiling_features" || true)
if [ "${existing:-0}" -gt 0 ]; then
  echo "[ceiling] stopping $existing existing ceiling_features"
  pgrep -f "argo_mini/ceiling_features" | xargs -r kill -9
  sleep 2
fi
pgrep -f "argo_mini/ceiling_map_builder" | xargs -r kill -9 2>/dev/null
sleep 1

echo "[ceiling] starting detector..."
ros2 run argo_mini ceiling_features 2>&1 | sed 's/^/[features] /' &
FEAT_PID=$!

for i in $(seq 1 30); do
  ros2 topic list 2>/dev/null | grep -q '/ceiling_features/lights' && break
  sleep 1
done

# Kill the nodes by name, not by the `ros2 run` wrapper's PID. Killing the
# wrapper leaves the actual node running, and an orphaned ceiling_features is
# invisible until the next run starts a second one and the builder quietly
# interleaves two ceiling planes. That is precisely how this went wrong once.
cleanup() {
  echo "[ceiling] stopping..."
  pgrep -f "argo_mini/ceiling_map_builder" | xargs -r kill -INT 2>/dev/null
  sleep 2
  pgrep -f "argo_mini/ceiling_map_builder" | xargs -r kill -9 2>/dev/null
  pgrep -f "argo_mini/ceiling_features"    | xargs -r kill -9 2>/dev/null
  kill $FEAT_PID 2>/dev/null
  wait 2>/dev/null
  echo "[ceiling] stopped."
}
trap cleanup EXIT INT TERM

echo ""
echo "========================================="
echo "  CEILING MAP"
echo "  map:     $MAP_PATH"
echo "  azimuth: $AZIMUTH deg"
echo "  extend:  $EXTEND"
echo "========================================="
echo "  Drive the restaurant. Revisit tables — a light needs 5 sightings"
echo "  before it is written out. Watch for 'saved N landmarks'."
echo "  Ctrl-C when done."
echo ""

ros2 run argo_mini ceiling_map_builder --ros-args \
  -p map_path:="$MAP_PATH" \
  -p ceiling_azimuth_deg:="$AZIMUTH" \
  -p extend_existing:="$EXTEND" 2>&1 | sed 's/^/[map] /'
