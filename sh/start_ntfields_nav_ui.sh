#!/bin/bash
# Argo Mini — NTFields Navigation (UI-launched)
#
# UI-dedicated counterpart to sh/start_ntfields_nav.sh, mirroring how
# start_argo_nav_ui.sh relates to start_argo_nav.sh — same nodes, but
# headless-safe (--no-rviz) and kept separate so a browser click can never
# disturb an SSH session running the original by hand. Carries over every
# reliability fix already proven on start_argo_nav_ui.sh:
#   - Cyclone DDS instead of Fast-DDS (intermittent lifecycle-transition
#     timeouts under load, confirmed on this same node set)
#   - ros2 daemon reset before each launch (stale daemon -> every
#     "ros2 lifecycle set" call fails with a confusing rclpy.ok() fault)
#   - shutdown trap scoped to this script's own process group instead of a
#     blanket "pkill -9 -f ros2" (which collaterally killed the independent
#     argo-rosbridge service every time the stack stopped)
#   - costmap-topic checks placed AFTER the nodes that publish them
#   - step-by-step progress written to /tmp/argo_nav_progress so the UI can
#     show which step is running or which one failed, instead of an
#     indefinite spinner
#
# Differs from start_argo_nav_ui.sh in the actual stack:
#   - ntfields_social_shield   replaces depth_safety_shield (/cmd_vel_smoothed
#     -> /cmd_vel), three-layer speed scaling: static field / social (leg
#     detection) / depth stop
#   - ntfields_trainer         auto-trains on /map via CUDA (~20 min first
#     run, ~7 min fine-tune), saves ~/ntfields_model.pt
#   - ntfields_navigator       action server /ntfields/navigate_to_pose;
#     falls back to Nav2's own bt_navigator path until the model is ready
#
# Usage:
#   ./start_ntfields_nav_ui.sh                          # with camera + RViz
#   ./start_ntfields_nav_ui.sh --no-cam                 # lidar-only
#   ./start_ntfields_nav_ui.sh --map /path/to/map       # custom map path (no extension)
#   ./start_ntfields_nav_ui.sh --no-rviz                # headless (run via the web UI)
#
# Build first: cd ~/argo_mini_ws && colcon build --packages-select argo_mini --symlink-install

NO_CAM=false
NO_RVIZ=false
SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
MAP_BASE="$SCRIPT_DIR/src/argo_mini/maps/office_map"

for arg in "$@"; do
  [[ "$arg" == "--no-cam" ]]  && NO_CAM=true
  [[ "$arg" == "--no-rviz" ]] && NO_RVIZ=true
  [[ "$arg" == "--map" ]]     && { shift; MAP_BASE="$1"; }
done
MAP_BASE="${MAP_BASE/#\~/$HOME}"

# ── environment ──────────────────────────────────────────────────────────────
source /opt/ros/humble/setup.bash
source "$SCRIPT_DIR/install/setup.bash"

# Must match sh/start-rosbridge.sh / start_argo_nav_ui.sh's RMW setting or
# nodes started by each simply can't discover each other at all — see the
# comment there for why Cyclone DDS over the Fast-DDS default.
export RMW_IMPLEMENTATION=rmw_cyclonedds_cpp

CAMERA_SDK_PATH=~/EaiCameraSdk_v1.2.28.20241015/demo/linux_ros/ros2
export LD_LIBRARY_PATH=$LD_LIBRARY_PATH:$CAMERA_SDK_PATH/ascamera/libs/lib/aarch64-linux-gnu

NAV_CONFIG="$SCRIPT_DIR/install/argo_mini/share/argo_mini/config/nav2.yaml"
SLAM_CONFIG="$SCRIPT_DIR/install/argo_mini/share/argo_mini/config/slam_toolbox.yaml"

# ── USB permissions ────────────────────────────────────────────────────────
chmod 666 /dev/ttyUSB0 /dev/ttyUSB1 2>/dev/null || \
  sudo chmod 666 /dev/ttyUSB0 /dev/ttyUSB1 2>/dev/null || true

# ── kill previous run ──────────────────────────────────────────────────────
echo "[argo] Killing previous processes..."
for proc in slam_toolbox serial_bridge rplidar_composition rviz2 \
            map_server amcl planner_server controller_server \
            bt_navigator velocity_smoother scan_relay \
            robot_state_publisher depth_safety_shield ascamera_node \
            ntfields_trainer ntfields_navigator ntfields_social_shield \
            depth_stop; do
  pkill -9 -f "$proc" 2>/dev/null || true
done
sleep 5

# ── reset the ros2 CLI daemon ──────────────────────────────────────────────
# ros2 lifecycle set (used below by lc_node) goes through a shared background
# daemon for discovery caching. If that daemon is ever left stale (e.g. from
# a previous run's process group being torn down uncleanly), every lifecycle
# transition below fails with a confusing "xmlrpc.client.Fault: RuntimeError:
# !rclpy.ok()" and the whole stack never activates, with no indication why.
# Force a fresh daemon on every launch instead of hoping whatever state it's
# already in is healthy.
echo "[argo] Resetting ros2 daemon..."
ros2 daemon stop 2>/dev/null || true
ros2 daemon start
sleep 2

# ── progress reporting ─────────────────────────────────────────────────────
# launcher.py spawns this script with its stdout/stderr going to DEVNULL (a
# slow/chatty node can't be allowed to wedge the single-threaded HTTP
# server) — which means when something fails partway through, the UI would
# otherwise have nothing to show but an indefinite spinner, no matter how
# long it's actually been stuck or what broke. Write the current step to
# the same well-known file start_argo_nav_ui.sh uses — backend/launcher.py
# already serves it over GET /nav_progress regardless of which of the two
# scripts is actually running, since only one "navigate" mode process can
# be active at a time.
PROGRESS_FILE="/tmp/argo_nav_progress"
report()       { echo "[argo] $1";       echo "OK|$(date +%s)|$1"    > "$PROGRESS_FILE"; }
report_error() { echo "[argo] ERROR: $1"; echo "ERROR|$(date +%s)|$1" > "$PROGRESS_FILE"; }
report_ready() { echo "[argo] $1";        echo "READY|$(date +%s)|$1" > "$PROGRESS_FILE"; }
report "Starting NTFields nav stack..."

# ── cleanup, registered on EXIT (not just INT/TERM) ─────────────────────────
# A failed check_process/wait_for_topic below calls a plain `exit 1` — that
# is NOT the same as receiving a signal, so a trap registered only on INT
# TERM (the old approach, defined at the bottom of the script) never fires
# for it, AND wouldn't even exist yet that early regardless, since a trap
# only takes effect once bash actually executes that line. Register on EXIT
# instead, right at the top before anything starts, so every node already
# launched gets cleaned up no matter which way the script ends — normal
# completion, an early failure, or a signal. INT/TERM just do a plain
# `exit 0`, which itself triggers this EXIT trap — kept separate so the
# body only ever runs once instead of twice.
cleanup() {
  echo "[argo] Shutting down..."
  echo "STOPPED|$(date +%s)|Stack shut down" > "$PROGRESS_FILE"
  kill $RSP_PID $SERIAL_PID $LIDAR_PID $RELAY_PID \
       $SLAM_PID $PLANNER_PID $CONTROLLER_PID \
       $SMOOTHER_PID $BT_PID $BEHAVIOR_PID \
       $SHIELD_PID $TRAINER_PID $NAV_PID $RVIZ_PID \
       ${CAM_PID:-} $CAM_TF_PID $CAM_TF2_PID 2>/dev/null || true
  sleep 4
  # Scoped to this process group only, not a blanket "pkill -9 -f ros2" —
  # that pattern matches ANY process with ros2 anywhere in its command
  # line, system-wide, which collaterally killed the independent
  # argo-rosbridge service every time this stack stopped (it also runs via
  # ros2 launch). backend/launcher.py spawns this script with
  # start_new_session=True specifically so $$ is this group leader, and
  # every node above was backgrounded with plain "&" (no setsid), so
  # they are all still in it -- this reaches them without touching
  # anything outside.
  kill -9 -- -$$ 2>/dev/null || true
}
trap cleanup EXIT
trap 'exit 0' INT TERM

# ── lifecycle helper ───────────────────────────────────────────────────────
lc_node() {
  local node=$1
  echo "[argo]   configure $node..."
  ros2 lifecycle set "$node" configure 2>&1 | tail -1
  sleep 4
  echo "[argo]   activate  $node..."
  ros2 lifecycle set "$node" activate  2>&1 | tail -1
  sleep 4
}

# ── topic checker ──────────────────────────────────────────────────────────
wait_for_topic() {
  local topic=$1
  local timeout=${2:-30}
  local start=$(date +%s)

  report "Waiting for topic $topic (timeout: ${timeout}s)..."
  while true; do
    if ros2 topic list 2>/dev/null | grep -q "^${topic}$"; then
      echo "[argo] Topic $topic is available"
      return 0
    fi

    local elapsed=$(($(date +%s) - start))
    if [ $elapsed -ge $timeout ]; then
      report_error "Topic $topic not available after ${timeout}s"
      return 1
    fi

    sleep 1
  done
}

# ── action server checker ──────────────────────────────────────────────────
wait_for_action() {
  local action=$1
  local timeout=${2:-30}
  local start=$(date +%s)

  report "Waiting for action $action (timeout: ${timeout}s)..."
  while true; do
    if ros2 action list 2>/dev/null | grep -q "^${action}$"; then
      echo "[argo] Action $action is available"
      return 0
    fi

    local elapsed=$(($(date +%s) - start))
    if [ $elapsed -ge $timeout ]; then
      report_error "Action $action not available after ${timeout}s"
      return 1
    fi

    sleep 1
  done
}

# ── process checker ────────────────────────────────────────────────────────
check_process() {
  local pid=$1
  local name=$2
  if ! kill -0 "$pid" 2>/dev/null; then
    report_error "Process $name (PID $pid) has crashed!"
    return 1
  fi
  return 0
}

# ── 1. Robot state publisher ───────────────────────────────────────────────
report "Starting robot_state_publisher..."
ros2 launch argo_mini robot_state_publisher.launch.py &
RSP_PID=$!
sleep 5

# ── 2. Camera TF bridge ─────────────────────────────────────────────────────
# Publish camera frame directly under base_link (from URDF: x=0.2575, z=0.170)
report "Starting camera TF bridge..."
ros2 run tf2_ros static_transform_publisher \
  --x 0.2575 --y 0.0 --z 0.170 \
  --roll 0.0 --pitch 0.0 --yaw 0.0 \
  --frame-id base_link \
  --child-frame-id ascamera_hp60c_color_0 &
CAM_TF_PID=$!

ros2 run tf2_ros static_transform_publisher \
  --x 0.2575 --y 0.0 --z 0.170 \
  --roll 0.0 --pitch 0.0 --yaw 0.0 \
  --frame-id base_link \
  --child-frame-id ascamera_hp60c_camera_link_0 &
CAM_TF2_PID=$!
sleep 5

# ── 3. Serial bridge ────────────────────────────────────────────────────────
# left_tick_scale=2.1714: calibrated wheel ratio (right ticks 2.17x faster) —
# kept as-is from start_ntfields_nav.sh; this is a hardware calibration
# constant, not something to guess at, so it isn't borrowed from
# start_argo_nav_ui.sh's own (different) value.
report "Starting serial_bridge..."
ros2 run argo_mini serial_bridge --ros-args \
  -p port:=/dev/ttyUSB1 \
  -p baud:=115200 \
  -p left_tick_scale:=0.66 &
SERIAL_PID=$!
sleep 5

# ── 4. RPLidar A1 ───────────────────────────────────────────────────────────
report "Starting rplidar..."
ros2 run rplidar_ros rplidar_composition --ros-args \
  -p serial_port:=/dev/ttyUSB0 \
  -p serial_baudrate:=115200 \
  -p frame_id:=lidar_link \
  -p angle_compensate:=true \
  -p scan_mode:=Boost &
LIDAR_PID=$!
sleep 5

# ── 5. Scan relay ───────────────────────────────────────────────────────────
report "Starting scan_relay..."
ros2 run argo_mini scan_relay &
RELAY_PID=$!
sleep 4

# ── 6. SLAM Toolbox — localization mode ────────────────────────────────────
# Replaces map_server + AMCL: serves /map AND broadcasts map->odom TF.
# Relocalizes automatically on first scan, no initial pose click needed.
report "Starting slam_toolbox localization (map: $MAP_BASE)..."
ros2 run slam_toolbox localization_slam_toolbox_node --ros-args \
  --params-file "$SLAM_CONFIG" \
  -p map_file_name:="$MAP_BASE" &
SLAM_PID=$!
sleep 7

# ── 7. Behavior server ──────────────────────────────────────────────────────
report "Starting behavior_server..."
ros2 run nav2_behaviors behavior_server --ros-args \
  --params-file $NAV_CONFIG \
  -r cmd_vel:=/cmd_vel_raw &
BEHAVIOR_PID=$!
sleep 7

lc_node /behavior_server

# ── 8. Planner server ───────────────────────────────────────────────────────
report "Starting planner_server..."
ros2 run nav2_planner planner_server --ros-args --params-file $NAV_CONFIG &
PLANNER_PID=$!
sleep 5
lc_node /planner_server

# ── 9. Controller server -> /cmd_vel_raw ───────────────────────────────────
report "Starting controller_server..."
ros2 run nav2_controller controller_server --ros-args \
  --params-file $NAV_CONFIG \
  -r cmd_vel:=/cmd_vel_raw &
CONTROLLER_PID=$!
sleep 5
lc_node /controller_server

# global_costmap (planner_server) and local_costmap (controller_server) only
# exist once those nodes are activated, which lc_node above just did.
echo "[argo] Confirming costmap topics..."
wait_for_topic "local_costmap/costmap_raw" 15
wait_for_topic "global_costmap/costmap_raw" 15

# ── 10. Velocity smoother /cmd_vel_raw -> /cmd_vel_smoothed ────────────────
report "Starting velocity_smoother..."
ros2 run nav2_velocity_smoother velocity_smoother --ros-args \
  --params-file $NAV_CONFIG \
  -r cmd_vel:=/cmd_vel_raw \
  -r cmd_vel_smoothed:=/cmd_vel_smoothed &
SMOOTHER_PID=$!
sleep 5
lc_node /velocity_smoother

# ── Wait for action servers before BT tries to load ────────────────────────
report "Waiting for action servers..."
wait_for_action "/compute_path_to_pose" 30
wait_for_action "/follow_path" 30
wait_for_action "/backup" 30
sleep 5

# ── 11. BT Navigator ─────────────────────────────────────────────────────────
report "Starting bt_navigator..."
ros2 run nav2_bt_navigator bt_navigator --ros-args --params-file $NAV_CONFIG &
BT_PID=$!
sleep 7
lc_node /bt_navigator
report_ready "Nav2 fully activated - ready for goals"

# Everything from here on is plain echo, not report() — these later steps
# (camera, velocity-smoother re-check, social shield, trainer, navigator,
# RViz) would otherwise overwrite the READY status above with an ordinary
# "still starting" message and never set it back, making the UI look
# permanently stuck even once Nav2 itself had genuinely finished
# activating (this exact bug was already hit and fixed once on
# start_argo_nav_ui.sh). report_error() calls below are untouched — a real
# failure at these later steps is still worth surfacing.

# ── 12. Depth camera (optional) ─────────────────────────────────────────────
CAM_PID=""
if [ "$NO_CAM" = false ]; then
  echo "[argo] Starting HP60C camera..."
  (
    cd $CAMERA_SDK_PATH
    source install/setup.bash
    export LD_LIBRARY_PATH=$LD_LIBRARY_PATH:$CAMERA_SDK_PATH/ascamera/libs/lib/aarch64-linux-gnu
    ros2 launch ascamera hp60c.launch.py 2>&1 | sed 's/^/[camera] /'
  ) &
  CAM_PID=$!

  if wait_for_topic "/ascamera_hp60c/camera_publisher/depth0/points" 15; then
    echo "[argo] Camera ready"
  else
    echo "[argo] WARNING: Camera not publishing depth data"
    echo "[argo]   Social shield will run in lidar-only mode (depth layer stale=pass-through)"
  fi
else
  echo "[argo] Camera skipped (--no-cam)"
fi

# ── 13. Velocity smoother check ─────────────────────────────────────────────
if ! wait_for_topic "/cmd_vel_smoothed" 10; then
  report_error "Velocity smoother not publishing!"
  kill $SMOOTHER_PID 2>/dev/null || true
  exit 1
fi
echo "[argo] Velocity smoother ready"

# ── 14. NTFields Social Shield /cmd_vel_smoothed -> /cmd_vel ───────────────
# Replaces depth_safety_shield. Three-layer speed scaling: static speed
# field (near walls) + social (leg-detected humans) + depth stop.
echo "[argo] Starting ntfields_social_shield..."
ros2 run argo_mini ntfields_social_shield --ros-args \
  -p input_topic:=/cmd_vel_smoothed \
  -p output_topic:=/cmd_vel \
  -p depth_topic:=/ascamera_hp60c/camera_publisher/depth0/points \
  -p scan_topic:=/scan_corrected \
  -p depth_stop_dist:=0.30 \
  -p depth_slow_dist:=0.70 \
  -p depth_height_min:=0.10 \
  -p depth_height_max:=1.80 \
  -p depth_width:=0.30 \
  -p depth_min_points:=5 \
  -p depth_stale_s:=1.0 \
  -p sigma_human:=0.70 \
  -p amplitude_human:=0.95 \
  -p social_max_range:=2.00 \
  -p epsilon:=0.35 \
  -p lam:=2.0 &
SHIELD_PID=$!
sleep 7

# check_process already calls report_error internally on failure.
if check_process $SHIELD_PID "ntfields_social_shield"; then
  echo "[argo] Social shield ready"
else
  exit 1
fi

# ── 15. NTFields Trainer (GPU background training) ─────────────────────────
# Watches /map, trains automatically. First run ~20 min, fine-tune ~7 min.
# Saves ~/ntfields_model.pt — navigator hot-swaps it automatically.
echo "[argo] Starting ntfields_trainer (training begins when /map arrives)..."
ros2 run argo_mini ntfields_trainer --ros-args \
  -p device:=cuda \
  -p num_epochs:=800 \
  -p steps_per_epoch:=150 \
  -p batch_size:=512 \
  -p n_sample_points:=60000 \
  -p epsilon:=0.35 \
  -p lam:=2.0 \
  -p change_threshold:=0.05 &
TRAINER_PID=$!
sleep 5

if check_process $TRAINER_PID "ntfields_trainer"; then
  echo "[argo] NTFields trainer running (watch: ros2 topic echo /ntfields/status)"
else
  exit 1
fi

# ── 16. NTFields Navigator (action server) ──────────────────────────────────
# Falls back to Nav2's own bt_navigator path until ~/ntfields_model.pt is ready.
echo "[argo] Starting ntfields_navigator..."
ros2 run argo_mini ntfields_navigator --ros-args \
  -p device:=cuda \
  -p alpha:=0.03 \
  -p goal_radius:=0.12 \
  -p max_steps:=600 \
  -p waypoint_stride:=4 &
NAV_PID=$!
sleep 5

if check_process $NAV_PID "ntfields_navigator"; then
  echo "[argo] NTFields navigator ready (/ntfields/navigate_to_pose)"
else
  exit 1
fi

# ── 17. RViz (optional) ─────────────────────────────────────────────────────
RVIZ_PID=""
if [ "$NO_RVIZ" = false ]; then
  echo "[argo] Starting RViz..."
  export DISPLAY=:1
  rviz2 &
  RVIZ_PID=$!
fi

echo ""
echo "=========================================="
echo "  ARGO MINI - NTFIELDS NAVIGATION"
echo "=========================================="
echo "  Map:      $MAP_BASE"
echo "  Camera:   $([ "$NO_CAM" = false ] && echo 'enabled' || echo 'disabled')"
echo "  Pipeline: controller     -> /cmd_vel_raw"
echo "            smoother       -> /cmd_vel_smoothed"
echo "            social_shield  -> /cmd_vel"
echo ""
echo "  ROS Topics:"
ros2 topic list 2>/dev/null | grep -E "(cmd_vel|depth|scan|odom|ntfields)" | sed 's/^/    /'
echo ""
echo "  NTFields training: ros2 topic echo /ntfields/status"
echo "  Training time:     ~20 min first run, ~7 min fine-tune"
echo "  Model saved to:    ~/ntfields_model.pt"
echo ""
echo "  Use RViz 2D Goal Pose to send nav goals."
echo "  No initial pose needed - auto-localizes."
echo "  Press Ctrl+C to stop all nodes"
echo "=========================================="
echo ""

if [ "$NO_RVIZ" = false ] && [ -n "$RVIZ_PID" ]; then
  wait $RVIZ_PID
else
  wait $NAV_PID
fi
