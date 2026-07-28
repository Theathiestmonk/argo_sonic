#!/bin/bash
# Argo Mini ? Navigation with SLAM Toolbox localization + depth safety shield
#
# Usage:
#   ./start_argo_nav_ui.sh                          # with camera + RViz
#   ./start_argo_nav_ui.sh --no-cam                 # lidar-only
#   ./start_argo_nav_ui.sh --map /path/to/map       # custom map path (no extension)
#   ./start_argo_nav_ui.sh --no-rviz                # headless (run via the web UI)
#
# Default map: ~/maps/indoor_map
# Create the map first with sh/start_slam.sh

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

CAMERA_SDK_PATH=~/EaiCameraSdk_v1.2.28.20241015/demo/linux_ros/ros2
export LD_LIBRARY_PATH=$LD_LIBRARY_PATH:$CAMERA_SDK_PATH/ascamera/libs/lib/aarch64-linux-gnu

NAV_CONFIG="$SCRIPT_DIR/install/argo_mini/share/argo_mini/config/nav2.yaml"
SLAM_CONFIG="$SCRIPT_DIR/install/argo_mini/share/argo_mini/config/slam_toolbox.yaml"

# ?? USB permissions ????????????????????????????????????????????????????????
chmod 666 /dev/ttyUSB0 /dev/ttyUSB1 2>/dev/null || \
  sudo chmod 666 /dev/ttyUSB0 /dev/ttyUSB1 2>/dev/null || true

# ?? kill previous run ??????????????????????????????????????????????????????
echo "[argo] Killing previous processes..."
for proc in slam_toolbox serial_bridge rplidar_composition rviz2 \
            map_server amcl planner_server controller_server \
            bt_navigator velocity_smoother scan_relay \
            robot_state_publisher depth_safety_shield ascamera_node; do
  pkill -9 -f "$proc" 2>/dev/null || true
done
sleep 5

# ?? reset the ros2 CLI daemon ?????????????????????????????????????????????
# ros2 lifecycle set (used below by lc_node) goes through a shared background
# daemon for discovery caching. If that daemon is ever left stale (e.g. from
# a previous run's process group being torn down uncleanly), every lifecycle
# transition below fails with a confusing "xmlrpc.client.Fault: RuntimeError:
# !rclpy.ok()" and the whole stack never activates, with no indication why —
# this cost real debugging time once already. Force a fresh daemon on every
# launch instead of hoping whatever state it's already in is healthy.
echo "[argo] Resetting ros2 daemon..."
ros2 daemon stop 2>/dev/null || true
ros2 daemon start
sleep 2

# ?? lifecycle helper ???????????????????????????????????????????????????????
lc_node() {
  local node=$1
  echo "[argo]   configure $node..."
  ros2 lifecycle set "$node" configure 2>&1 | tail -1
  sleep 4
  echo "[argo]   activate  $node..."
  ros2 lifecycle set "$node" activate  2>&1 | tail -1
  sleep 4
}

# ?? topic checker ??????????????????????????????????????????????????????????
wait_for_topic() {
  local topic=$1
  local timeout=${2:-30}
  local start=$(date +%s)

  echo "[argo] Waiting for topic $topic (timeout: ${timeout}s)..."
  while true; do
    if ros2 topic list 2>/dev/null | grep -q "^${topic}$"; then
      echo "[argo] ? Topic $topic is available"
      return 0
    fi

    local elapsed=$(($(date +%s) - start))
    if [ $elapsed -ge $timeout ]; then
      echo "[argo] ? ERROR: Topic $topic not available after ${timeout}s"
      return 1
    fi

    sleep 1
  done
}

# ?? action server checker ????????????????????????????????????????????????????
wait_for_action() {
  local action=$1
  local timeout=${2:-30}
  local start=$(date +%s)

  echo "[argo] Waiting for action $action (timeout: ${timeout}s)..."
  while true; do
    if ros2 action list 2>/dev/null | grep -q "^${action}$"; then
      echo "[argo] ? Action $action is available"
      return 0
    fi

    local elapsed=$(($(date +%s) - start))
    if [ $elapsed -ge $timeout ]; then
      echo "[argo] ? ERROR: Action $action not available after ${timeout}s"
      return 1
    fi

    sleep 1
  done
}

# ?? process checker ???????????????????????????????????????????????????????
check_process() {
  local pid=$1
  local name=$2
  if ! kill -0 "$pid" 2>/dev/null; then
    echo "[argo] ? ERROR: Process $name (PID $pid) has crashed!"
    return 1
  fi
  return 0
}

# ?? 1. Robot state publisher ???????????????????????????????????????????????
echo "[argo] Starting robot_state_publisher..."
ros2 launch argo_mini robot_state_publisher.launch.py &
RSP_PID=$!
sleep 5

# ?? 2. Camera TF bridge ????????????????????????????????????????????????????
# Publish camera frame directly under base_link (from URDF: x=0.2575, z=0.170)
echo "[argo] Starting camera TF bridge..."
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

# ?? 3. Serial bridge ??????????????????????????????????????????????????????
# left_tick_scale=2.1714: calibrated wheel ratio (right ticks 2.17x faster)
# fixed_dac=106: constant DAC ? consistent tick rate ? cleaner odometry
echo "[argo] Starting serial_bridge..."
ros2 run argo_mini serial_bridge --ros-args \
  -p port:=/dev/ttyUSB1 \
  -p baud:=115200 \
  -p left_tick_scale:=0.66 &
SERIAL_PID=$!
sleep 5

# ?? 4. RPLidar A1 ?????????????????????????????????????????????????????????
echo "[argo] Starting rplidar..."
ros2 run rplidar_ros rplidar_composition --ros-args \
  -p serial_port:=/dev/ttyUSB0 \
  -p serial_baudrate:=115200 \
  -p frame_id:=lidar_link \
  -p angle_compensate:=true \
  -p scan_mode:=Boost &
LIDAR_PID=$!
sleep 5

# ?? 5. Scan relay ??????????????????????????????????????????????????????????
echo "[argo] Starting scan_relay..."
ros2 run argo_mini scan_relay &
RELAY_PID=$!
sleep 4

# ?? 6. SLAM Toolbox ? localization mode ???????????????????????????????????
# Replaces map_server + AMCL: serves /map AND broadcasts map?odom TF.
# Relocalizes automatically on first scan ? no initial pose click needed.
echo "[argo] Starting slam_toolbox localization (map: $MAP_BASE)..."
ros2 run slam_toolbox localization_slam_toolbox_node --ros-args \
  --params-file "$SLAM_CONFIG" \
  -p map_file_name:="$MAP_BASE" &
SLAM_PID=$!
sleep 7

# ?? 7. Behavior server ????????????????????????????????????????????????????
echo "[argo] Starting behavior_server..."
ros2 run nav2_behaviors behavior_server --ros-args \
  --params-file $NAV_CONFIG \
  -r cmd_vel:=/cmd_vel_raw &
BEHAVIOR_PID=$!
sleep 7

lc_node /behavior_server

# ?? 8. Planner server ?????????????????????????????????????????????????????
echo "[argo] Starting planner_server..."
ros2 run nav2_planner planner_server --ros-args --params-file $NAV_CONFIG &
PLANNER_PID=$!
sleep 5
lc_node /planner_server

# ?? 9. Controller server ? /cmd_vel_raw ???????????????????????????????????
echo "[argo] Starting controller_server..."
ros2 run nav2_controller controller_server --ros-args \
  --params-file $NAV_CONFIG \
  -r cmd_vel:=/cmd_vel_raw &
CONTROLLER_PID=$!
sleep 5
lc_node /controller_server

# global_costmap (planner_server) and local_costmap (controller_server) only
# exist once those nodes are activated, which lc_node above just did — this
# used to run BEFORE either node was even started (right after
# behavior_server), so both checks failed every single run, unconditionally.
# Confirm now, at the point where they can actually be true.
echo "[argo] Confirming costmap topics..."
wait_for_topic "local_costmap/costmap_raw" 15
wait_for_topic "global_costmap/costmap_raw" 15

# ?? 10. Velocity smoother /cmd_vel_raw ? /cmd_vel_smoothed ????????????????
echo "[argo] Starting velocity_smoother..."
ros2 run nav2_velocity_smoother velocity_smoother --ros-args \
  --params-file $NAV_CONFIG \
  -r cmd_vel:=/cmd_vel_raw \
  -r cmd_vel_smoothed:=/cmd_vel_smoothed &
SMOOTHER_PID=$!
sleep 5
lc_node /velocity_smoother

# ?? Wait for action servers to be ready before BT tries to load ??????????????
echo "[argo] Waiting for action servers..."
wait_for_action "/compute_path_to_pose" 30
wait_for_action "/follow_path" 30
wait_for_action "/backup" 30
sleep 5

# ?? 11. BT Navigator ??????????????????????????????????????????????????????
echo "[argo] Starting bt_navigator..."
ros2 run nav2_bt_navigator bt_navigator --ros-args --params-file $NAV_CONFIG &
BT_PID=$!
sleep 7
lc_node /bt_navigator

# ?? 12. Depth camera (optional) ???????????????????????????????????????????
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

  # Wait for camera to publish depth data
  if wait_for_topic "/ascamera_hp60c/camera_publisher/depth0/points" 15; then
    echo "[argo] ? Camera ready"
  else
    echo "[argo] ? WARNING: Camera not publishing depth data"
    echo "[argo]   Check: USB connection, SDK libraries, camera permissions"
    # Don't exit?safety shield can run with STALE state (pass-through)
  fi
else
  echo "[argo] Camera skipped (--no-cam)"
fi

# ?? 13. Velocity smoother check ????????????????????????????????
if ! wait_for_topic "/cmd_vel_smoothed" 10; then
  echo "[argo] ? ERROR: Velocity smoother not publishing!"
  kill $SMOOTHER_PID 2>/dev/null || true
  exit 1
fi
echo "[argo] ? Velocity smoother ready"

# ?? 14. Depth safety shield /cmd_vel_smoothed ? /cmd_vel ??????????????????
echo "[argo] Starting depth_safety_shield..."
ros2 run argo_mini depth_safety_shield --ros-args \
  -p stop_distance:=0.60 \
  -p tunnel_width:=0.30 \
  -p min_points:=30 \
  -p height_min:=-0.40 \
  -p height_max:=0.40 \
  -p use_optical_frame:=true \
  -p input_topic:=/cmd_vel_smoothed \
  -p output_topic:=/cmd_vel \
  -p depth_topic:=/ascamera_hp60c/camera_publisher/depth0/points &
SHIELD_PID=$!
sleep 7

# Verify safety shield is running
if check_process $SHIELD_PID "depth_safety_shield"; then
  echo "[argo] ? Safety shield ready"
else
  echo "[argo] ? ERROR: Safety shield failed to start"
  exit 1
fi

# ── 15. RViz (optional) ───────────────────────────────────────────────────
RVIZ_PID=""
if [ "$NO_RVIZ" = false ]; then
  echo "[argo] Starting RViz..."
  export DISPLAY=:1
  rviz2 &
  RVIZ_PID=$!
fi

echo ""
echo "========================================="
echo "  ARGO MINI ? NAV2 + SLAM LOCALIZATION"
echo "========================================="
echo "  Map:      $MAP_BASE"
echo "  Camera:   $([ "$NO_CAM" = false ] && echo 'enabled' || echo 'disabled')"
echo "  Pipeline: controller ? /cmd_vel_raw"
echo "            smoother   ? /cmd_vel_smoothed"
echo "            shield     ? /cmd_vel"
echo ""
echo "  ROS Topics:"
ros2 topic list 2>/dev/null | grep -E "(cmd_vel|depth|scan|odom)" | sed 's/^/    /'
echo ""
echo "  Use RViz 2D Goal Pose to send nav goals."
echo "  No initial pose needed ? auto-localizes."
echo "  Press Ctrl+C to stop all nodes"
echo "========================================="
echo ""

trap '
  echo "[argo] Shutting down..."
  kill $RSP_PID $SERIAL_PID $LIDAR_PID $RELAY_PID \
       $SLAM_PID $PLANNER_PID $CONTROLLER_PID \
       $SMOOTHER_PID $BT_PID $BEHAVIOR_PID $SHIELD_PID $RVIZ_PID \
       ${CAM_PID:-} $CAM_TF_PID $CAM_TF2_PID 2>/dev/null || true
  sleep 4
  # Was "pkill -9 -f ros2" -- matches ANY process with ros2 anywhere in its
  # command line, system-wide, which collaterally kills the independent
  # argo-rosbridge systemd service too (it also runs via ros2 launch).
  # Scope the stragglers-still-alive fallback to just this process group
  # instead: backend/launcher.py spawns this script with start_new_session
  # equal to True specifically so $$ is that group leader, and every node
  # above was backgrounded with plain ampersand (no setsid), so they are all
  # still in it -- this reaches them without touching anything outside.
  kill -9 -- -$$ 2>/dev/null || true
  exit 0
' INT TERM

if [ "$NO_RVIZ" = false ] && [ -n "$RVIZ_PID" ]; then
  wait $RVIZ_PID
else
  wait $SHIELD_PID
fi
