#!/bin/bash
# Argo Mini — NTFields Navigation (fast, daemon-free launcher)
#
# Bash port of argo_sonic_nav.py's stack (NTFields planner as /planner_server,
# pose_init, pointcloud_restamper, safety_shield — NOT the ntfields_trainer/
# ntfields_social_shield/ntfields_navigator variant used by
# start_ntfields_nav_ui.sh, which is a different stack). Written because the
# Python launcher's own TUI/subprocess plumbing is a second thing that can
# break independently of the ROS2 stack it's driving — a plain bash script
# has far fewer moving parts.
#
# Never touches `ros2 daemon` at all (no stop/start, no bare `ros2 action
# list`), unlike every other start_*.sh in this repo:
#   - `ros2 lifecycle set/get` already accept --no-daemon (used throughout).
#   - `ros2 action list` has NO --no-daemon option and would spawn/depend on
#     a daemon regardless — so action-server readiness is checked via the
#     status topic every rclcpp/rclpy action server publishes automatically
#     (<action>/_action/status), using `ros2 topic list --no-daemon` instead.
#   - Net effect: no "stale daemon -> every lifecycle call fails with
#     rclpy.ok() fault" class of bug is even possible, and no fixed
#     daemon-reset delay (was ~4s of dead time on every run elsewhere).
#
# Usage:
#   ./start_ntfields_nav_fast.sh                          # with camera + RViz
#   ./start_ntfields_nav_fast.sh --no-cam                 # lidar-only
#   ./start_ntfields_nav_fast.sh --no-rviz                # headless (web UI)
#   ./start_ntfields_nav_fast.sh --map /path/to/map       # custom map (no extension)
#
# Build first: cd ~/my_project/argo_sonic && colcon build --symlink-install

NO_CAM=false
NO_RVIZ=false
SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
MAP_BASE="$SCRIPT_DIR/src/argo_mini/maps/Atsn_cafe_map"

for arg in "$@"; do
  [[ "$arg" == "--no-cam" ]]  && NO_CAM=true
  [[ "$arg" == "--no-rviz" ]] && NO_RVIZ=true
  [[ "$arg" == "--map" ]]     && { shift; MAP_BASE="$1"; }
done
MAP_BASE="${MAP_BASE/#\~/$HOME}"

# ── environment ──────────────────────────────────────────────────────────────
source /opt/ros/humble/setup.bash
source "$SCRIPT_DIR/install/setup.bash"

# Matches every other start_*.sh's RMW setting — required for nodes started
# by this script to discover each other at all.
export RMW_IMPLEMENTATION=rmw_cyclonedds_cpp

CAMERA_SDK_PATH=~/EaiCameraSdk_v1.2.28.20241015/demo/linux_ros/ros2
export LD_LIBRARY_PATH=$LD_LIBRARY_PATH:$CAMERA_SDK_PATH/ascamera/libs/lib/aarch64-linux-gnu

NAV_CONFIG="$SCRIPT_DIR/install/argo_mini/share/argo_mini/config/nav2.yaml"
SLAM_CONFIG="$SCRIPT_DIR/install/argo_mini/share/argo_mini/config/slam_toolbox.yaml"
NTFIELDS_CONFIG="$SCRIPT_DIR/install/argo_mini/share/argo_mini/config/ntfields.yaml"
NTFIELDS_MODEL="${NTFIELDS_MODELS_DIR:-$HOME/ntfields_models}/$(basename "$MAP_BASE").pt"
BT_XML="$SCRIPT_DIR/src/argo_mini/config/bt/navigate_to_pose.xml"

# ── USB permissions ────────────────────────────────────────────────────────
chmod 666 /dev/esp32 /dev/lidar 2>/dev/null || \
  sudo chmod 666 /dev/esp32 /dev/lidar 2>/dev/null || true

# NOTE: if ntfields_planner_node ever hangs silently at the "Starting
# NTFields planner..." step again, it's very likely a stale/corrupted CUDA
# JIT cache (confirmed root cause once on this board) — fix with:
#   rm -rf ~/.nv/ComputeCache ~/.cache/torch_extensions
# Not run automatically here since it'd force a full JIT recompile (real
# startup cost) on every single launch. ntfields_planner_node.py's own
# 60s load timeout means this can no longer hang the whole stack forever
# even if it does recur — it'll fail cleanly with a clear log line instead.

# ── kill previous run ──────────────────────────────────────────────────────
# _ros2_daemon is included here (not reset separately later) since this
# script never calls `ros2 daemon stop/start` — any daemon left over from a
# previous run/process is simply killed like every other stale node below.
echo "[argo] Killing previous processes..."
for proc in slam_toolbox serial_bridge rplidar_composition rviz2 \
            ntfields_planner_node planner_server controller_server \
            bt_navigator velocity_smoother scan_relay pose_init \
            robot_state_publisher depth_safety_shield safety_shield \
            ascamera_node pointcloud_restamper behavior_server \
            _ros2_daemon; do
  pkill -9 -f "$proc" 2>/dev/null || true
done
sleep 2

# serial_bridge holds /dev/esp32 exclusively — force-free the port itself
# too, since a holder that doesn't show "serial_bridge" in its own command
# line survives the name-based pkill above untouched.
fuser -k -9 /dev/esp32 2>/dev/null || true
sleep 1

# ── progress reporting ─────────────────────────────────────────────────────
PROGRESS_FILE="/tmp/argo_nav_progress"
touch "$PROGRESS_FILE" 2>/dev/null || true
chmod 666 "$PROGRESS_FILE" 2>/dev/null || true
_READY=false
report()       { $_READY && return; echo "[argo] $1";       echo "OK|$(date +%s)|$1"    > "$PROGRESS_FILE"; }
report_error() { echo "[argo] ERROR: $1"; echo "ERROR|$(date +%s)|$1" > "$PROGRESS_FILE"; }
report_ready() { _READY=true; echo "[argo] $1"; echo "READY|$(date +%s)|$1" > "$PROGRESS_FILE"; }
report "Starting NTFields nav stack (fast/no-daemon)..."

# ── cleanup on any exit path ───────────────────────────────────────────────
cleanup() {
  echo "[argo] Shutting down..."
  echo "STOPPED|$(date +%s)|Stack shut down" > "$PROGRESS_FILE"
  kill $RSP_PID $CAM_TF_PID $SERIAL_PID $LIDAR_PID $RELAY_PID $SLAM_PID \
       $PLANNER_PID $CONTROLLER_PID $SMOOTHER_PID $BEHAVIOR_PID $BT_PID \
       $CAM_PID $RESTAMP_PID $SHIELD_PID $RVIZ_PID 2>/dev/null || true
  sleep 3
  # Scoped to this process group only — see start_argo_nav_ui.sh's cleanup
  # for why a blanket "pkill -9 -f ros2" is wrong (kills argo-rosbridge too).
  kill -9 -- -$$ 2>/dev/null || true
}
trap cleanup EXIT
trap 'exit 0' INT TERM

# ── helpers (all --no-daemon; action readiness via topic, not `action list`) ─
wait_for_topic() {
  local topic=$1 timeout=${2:-30} start=$(date +%s)
  report "Waiting for topic $topic (timeout: ${timeout}s)..."
  while true; do
    ros2 topic list --no-daemon 2>/dev/null | grep -q "^${topic}$" && { echo "[argo] Topic $topic ready"; return 0; }
    [ $(( $(date +%s) - start )) -ge $timeout ] && { report_error "Topic $topic not available after ${timeout}s"; return 1; }
    sleep 0.5
  done
}

wait_for_action() {
  local action=$1 timeout=${2:-30} start=$(date +%s)
  report "Waiting for action $action (timeout: ${timeout}s)..."
  while true; do
    ros2 topic list --no-daemon 2>/dev/null | grep -q "^${action}/_action/status$" && { echo "[argo] Action $action ready"; return 0; }
    [ $(( $(date +%s) - start )) -ge $timeout ] && { report_error "Action $action not available after ${timeout}s"; return 1; }
    sleep 0.5
  done
}

lc_state() {
  local node=$1 state=$2 timeout=${3:-30} start=$(date +%s)
  local target; case "$state" in inactive) target='[2]';; active) target='[3]';; esac
  while true; do
    ros2 lifecycle get "$node" --no-daemon 2>/dev/null | grep -q -- "$target" && return 0
    [ $(( $(date +%s) - start )) -ge $timeout ] && return 1
    sleep 0.5
  done
}

wait_for_node() {
  local node=$1 timeout=${2:-15} start=$(date +%s)
  while true; do
    ros2 node list --no-daemon 2>/dev/null | grep -qx "$node" && return 0
    [ $(( $(date +%s) - start )) -ge $timeout ] && return 1
    sleep 0.3
  done
}

lc_node() {
  local node=$1 cfg_timeout=${2:-30} act_timeout=${3:-25}
  # A fixed sleep before this call isn't reliable — a fresh process
  # importing torch can occasionally take a beat longer than expected, and
  # calling lifecycle set before the node has registered in the ROS graph
  # yet fails immediately with "Node not Found" (seen on this exact
  # sequence for /planner_server). Wait for the node to actually exist
  # first instead of guessing with a delay.
  if ! wait_for_node "$node" 20; then
    report_error "$node never appeared in 'ros2 node list' — process may have crashed on startup"
    return 1
  fi
  echo "[argo]   configure $node..."
  ros2 lifecycle set "$node" configure --no-daemon 2>&1 | tail -1
  lc_state "$node" inactive "$cfg_timeout" || echo "[argo]   WARN: $node configure timeout — proceeding anyway"
  echo "[argo]   activate  $node..."
  ros2 lifecycle set "$node" activate --no-daemon 2>&1 | tail -1
  if lc_state "$node" active "$act_timeout"; then
    echo "[argo]   $node active"
    return 0
  fi
  report_error "$node failed to activate"
  return 1
}

check_process() {
  kill -0 "$1" 2>/dev/null && return 0
  report_error "Process $2 (PID $1) has crashed!"
  return 1
}

# An RPLIDAR A1's motor needs a moment to spin up, and a start-scan command
# sent before that (or landing on stale UART bytes left by an immediately
# preceding attempt) fails with an SDK comms error indistinguishable from a
# real hardware fault — confirmed on this exact unit: dmesg showed no USB
# disconnect/reset around the failure, and a clean isolated retry succeeded.
# Kill and respin with growing backoff instead of failing the whole stack
# on one bad attempt.
launch_lidar_with_retry() {
  local attempts=3 settle=6 attempt pid start
  for attempt in $(seq 1 $attempts); do
    echo "[argo] Starting rplidar (attempt $attempt/$attempts)..."
    ros2 run rplidar_ros rplidar_composition --ros-args \
      -p serial_port:=/dev/lidar -p serial_baudrate:=115200 \
      -p frame_id:=lidar_link -p angle_compensate:=true -p scan_mode:=Boost &
    pid=$!
    start=$(date +%s)
    while [ $(( $(date +%s) - start )) -lt $settle ]; do
      kill -0 "$pid" 2>/dev/null || break
      ros2 topic list --no-daemon 2>/dev/null | grep -qx "/scan" && { LIDAR_PID=$pid; echo "[argo] rplidar ready"; return 0; }
      sleep 0.5
    done
    echo "[argo] rplidar did not come up cleanly (attempt $attempt/$attempts)"
    kill -9 "$pid" 2>/dev/null || true
    [ $attempt -lt $attempts ] && sleep $(( 2 * attempt ))
  done
  report_error "rplidar failed to come up after $attempts attempts"
  LIDAR_PID=""
  return 1
}

# ── 1. Robot state publisher ───────────────────────────────────────────────
report "Starting robot_state_publisher..."
ros2 launch argo_mini robot_state_publisher.launch.py &
RSP_PID=$!
sleep 2

# ── 2. Camera TF bridge ────────────────────────────────────────────────────
report "Starting camera TF bridge..."
ros2 run tf2_ros static_transform_publisher \
  --x 0.2575 --y 0.0 --z 0.170 --roll 0.417281 --pitch -0.018276 --yaw 1.611996 \
  --frame-id base_link --child-frame-id ascamera_hp60c_color_0 &
CAM_TF_PID=$!
sleep 1

# ── 3. Serial bridge ───────────────────────────────────────────────────────
report "Starting serial_bridge..."
ros2 run argo_mini serial_bridge --ros-args \
  -p port:=/dev/esp32 -p baud:=115200 -p left_tick_scale:=0.66 &
SERIAL_PID=$!
sleep 2

# ── 4. RPLidar A1 ───────────────────────────────────────────────────────────
report "Starting rplidar..."
launch_lidar_with_retry

# ── 5. Scan relay ───────────────────────────────────────────────────────────
report "Starting scan_relay..."
ros2 run argo_mini scan_relay &
RELAY_PID=$!
sleep 1

# ── 6. SLAM Toolbox (localization) ─────────────────────────────────────────
report "Starting slam_toolbox localization (map: $MAP_BASE)..."
ros2 run slam_toolbox localization_slam_toolbox_node --ros-args \
  --params-file "$SLAM_CONFIG" -p map_file_name:="$MAP_BASE" &
SLAM_PID=$!
wait_for_topic "/map" 40
sleep 1

# ── 6.5. Pose initializer ──────────────────────────────────────────────────
report "Initializing robot pose..."
ros2 run argo_mini pose_init --ros-args || echo "[argo] WARN: pose_init failed — check map JSON"
sleep 1

# ── 7. NTFields Planner (as /planner_server) ───────────────────────────────
report "Starting NTFields planner..."
ros2 run argo_mini ntfields_planner_node --ros-args \
  --params-file "$NTFIELDS_CONFIG" -p model_path:="$NTFIELDS_MODEL" &
PLANNER_PID=$!
sleep 3
lc_node /planner_server 30 20

# ── 8. Controller server ───────────────────────────────────────────────────
report "Starting controller_server..."
ros2 run nav2_controller controller_server --ros-args \
  --params-file "$NAV_CONFIG" -r cmd_vel:=/cmd_vel_raw &
CONTROLLER_PID=$!
sleep 2
lc_node /controller_server 30 20

echo "[argo] Confirming costmap topics..."
wait_for_topic "local_costmap/costmap_raw" 15
wait_for_topic "global_costmap/costmap_raw" 15

# ── 9. Velocity smoother ───────────────────────────────────────────────────
report "Starting velocity_smoother..."
ros2 run nav2_velocity_smoother velocity_smoother --ros-args \
  --params-file "$NAV_CONFIG" \
  -r cmd_vel:=/cmd_vel_raw -r cmd_vel_smoothed:=/cmd_vel_smoothed &
SMOOTHER_PID=$!
sleep 2
lc_node /velocity_smoother 25 20

# ── 10. Behavior server ────────────────────────────────────────────────────
report "Starting behavior_server..."
ros2 run nav2_behaviors behavior_server --ros-args \
  --params-file "$NAV_CONFIG" -r cmd_vel:=/cmd_vel_raw &
BEHAVIOR_PID=$!
sleep 2
lc_node /behavior_server 25 20

# ── Wait for action servers before BT tries to load ────────────────────────
wait_for_action "/follow_path" 30
wait_for_action "/backup" 30

# ── 11. BT Navigator ────────────────────────────────────────────────────────
report "Starting bt_navigator..."
ros2 run nav2_bt_navigator bt_navigator --ros-args --params-file "$NAV_CONFIG" \
  -p default_nav_to_pose_bt_xml:="$BT_XML" \
  -p default_nav_through_poses_bt_xml:="$BT_XML" &
BT_PID=$!
sleep 3
if lc_node /bt_navigator 30 20; then
  report_ready "Nav2 fully activated - ready for goals"
else
  report_error "bt_navigator failed to activate - /navigate_to_pose is not available"
fi

# ── 12. Depth camera (optional) ────────────────────────────────────────────
CAM_PID=""
if [ "$NO_CAM" = false ]; then
  echo "[argo] Starting HP60C camera..."
  (
    cd "$CAMERA_SDK_PATH"
    source install/setup.bash
    export LD_LIBRARY_PATH=$LD_LIBRARY_PATH:$CAMERA_SDK_PATH/ascamera/libs/lib/aarch64-linux-gnu
    ros2 launch ascamera hp60c.launch.py 2>&1 | sed 's/^/[camera] /'
  ) &
  CAM_PID=$!
  wait_for_topic "/ascamera_hp60c/camera_publisher/depth0/points" 15 || \
    echo "[argo] WARNING: Camera not publishing depth data"
else
  echo "[argo] Camera skipped (--no-cam)"
fi

wait_for_topic "/cmd_vel_smoothed" 10 || { echo "[argo] Velocity smoother not publishing – aborting"; exit 1; }

# ── 13. PointCloud restamper (optional) ────────────────────────────────────
RESTAMP_PID=""
if [ "$NO_CAM" = false ]; then
  echo "[argo] Starting pointcloud_restamper..."
  ros2 run argo_mini pointcloud_restamper &
  RESTAMP_PID=$!
  wait_for_topic "/ascamera_hp60c/camera_publisher/depth0/points_corrected" 10 || \
    echo "[argo] WARNING: points_corrected not publishing – voxel layer will have no depth data"
fi

# ── 14. Safety shield ───────────────────────────────────────────────────────
echo "[argo] Starting safety_shield..."
ros2 run argo_mini safety_shield &
SHIELD_PID=$!
sleep 2
check_process $SHIELD_PID "safety_shield" || exit 1

# ── RViz (optional) ────────────────────────────────────────────────────────
RVIZ_PID=""
if [ "$NO_RVIZ" = false ]; then
  echo "[argo] Starting RViz..."
  export DISPLAY=:1
  rviz2 &
  RVIZ_PID=$!
fi

echo ""
echo "=========================================="
echo "  ARGO MINI — NTFIELDS NAV (fast/no-daemon)"
echo "=========================================="
echo "  Map:      $MAP_BASE"
echo "  Camera:   $([ "$NO_CAM" = false ] && echo 'enabled' || echo 'disabled')"
echo "  Pipeline: controller -> /cmd_vel_raw -> smoother -> /cmd_vel_smoothed -> shield -> /cmd_vel"
echo "  Use RViz 2D Goal Pose to send nav goals."
echo "  Press Ctrl+C to stop all nodes"
echo "=========================================="
echo ""

if [ "$NO_RVIZ" = false ] && [ -n "$RVIZ_PID" ]; then
  wait $RVIZ_PID
else
  wait $SHIELD_PID
fi
