#!/bin/bash
# Argo Mini — Stepwise NTFields Navigation Launcher (self-verifying)
#
# Brings the stack up ONE node at a time, printing each node's real output
# live, then AUTOMATICALLY VERIFIES that step actually succeeded (topic
# exists / lifecycle node reached 'active') before asking you to press
# Enter for the next one. A failed check stops the whole script right
# there instead of silently continuing into a broken next step — built
# specifically to debug the case where a node hangs/fails with no visible
# error under the full launcher.
#
# Validated end-to-end on-robot (2026-09-09): steps 0-7 (through NTFields
# planner activation) confirmed working after clearing a stale CUDA JIT
# cache — see start_ntfields_nav_fast.sh's note on that. For routine runs,
# use start_ntfields_nav_fast.sh (same stack, fully automated, no manual
# confirmation needed); come back to this one specifically when something
# breaks and you need to see exactly which step and why.
#
# Usage: ./start_nav_stepwise.sh [--map /path/to/map]

SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
MAP_BASE="$SCRIPT_DIR/src/argo_mini/maps/office_map2"
for arg in "$@"; do
  [[ "$arg" == "--map" ]] && { shift; MAP_BASE="$1"; }
done
MAP_BASE="${MAP_BASE/#\~/$HOME}"

source /opt/ros/humble/setup.bash
source "$SCRIPT_DIR/install/setup.bash"
export RMW_IMPLEMENTATION=rmw_cyclonedds_cpp

CAMERA_SDK_PATH=~/EaiCameraSdk_v1.2.28.20241015/demo/linux_ros/ros2
export LD_LIBRARY_PATH=$LD_LIBRARY_PATH:$CAMERA_SDK_PATH/ascamera/libs/lib/aarch64-linux-gnu

NAV_CONFIG="$SCRIPT_DIR/install/argo_mini/share/argo_mini/config/nav2.yaml"
SLAM_CONFIG="$SCRIPT_DIR/install/argo_mini/share/argo_mini/config/slam_toolbox.yaml"
NTFIELDS_CONFIG="$SCRIPT_DIR/install/argo_mini/share/argo_mini/config/ntfields.yaml"
NTFIELDS_MODEL="${NTFIELDS_MODELS_DIR:-$HOME/ntfields_models}/$(basename "$MAP_BASE").pt"
BT_XML="$SCRIPT_DIR/src/argo_mini/config/bt/navigate_to_pose.xml"

pause() { echo; read -p ">>> Press Enter to start: $1  (Ctrl+C to abort)  " _; }

fail_stop() {
  echo "!! FAILED: $1"
  echo "!! Stopping here — fix this before re-running. Nothing after this step was started."
  exit 1
}

check_topic() {
  local topic=$1 timeout=${2:-15} start=$(date +%s)
  while [ $(( $(date +%s) - start )) -lt $timeout ]; do
    ros2 topic list --no-daemon 2>/dev/null | grep -qx "$topic" && { echo "-- OK: topic $topic is up."; return 0; }
    sleep 0.5
  done
  fail_stop "topic $topic never appeared within ${timeout}s"
}

check_process_alive() {
  kill -0 "$1" 2>/dev/null && { echo "-- OK: $2 (PID $1) is still running."; return 0; }
  fail_stop "$2 (PID $1) died/crashed — check its output above"
}

check_lifecycle_active() {
  local node=$1 timeout=${2:-30} start=$(date +%s)
  while [ $(( $(date +%s) - start )) -lt $timeout ]; do
    ros2 lifecycle get "$node" --no-daemon 2>/dev/null | grep -q '\[3\]' && { echo "-- OK: $node reached active."; return 0; }
    sleep 0.5
  done
  fail_stop "$node never reached 'active' state within ${timeout}s — check its output above"
}

echo "==================================================================="
echo " Stepwise nav stack — map: $MAP_BASE"
echo " NTFields model: $NTFIELDS_MODEL"
echo "==================================================================="

# ── Step 0: standalone model-load sanity check (no ROS at all) ─────────────
pause "Step 0 — standalone NTFields model load test (isolates CUDA/model issues from ROS)"
python3 -c "
import sys, time
sys.path.insert(0, '$SCRIPT_DIR/src/argo_mini/argo_mini')
from ntfields_model import NTFieldsModel
t0 = time.time()
print('constructing model on cuda...', flush=True)
model = NTFieldsModel(dim=2, device='cuda')
print(f'constructed in {time.time()-t0:.2f}s, loading checkpoint...', flush=True)
t1 = time.time()
norm = model.load('$NTFIELDS_MODEL')
print(f'loaded in {time.time()-t1:.2f}s, norm={norm}', flush=True)
" || fail_stop "standalone model load errored or hung — fix this before touching ROS at all"
echo "-- Step 0 OK -- model loads fine outside ROS."

chmod 666 /dev/esp32 /dev/lidar 2>/dev/null || sudo chmod 666 /dev/esp32 /dev/lidar 2>/dev/null || true

pkill -9 -f "slam_toolbox\|serial_bridge\|rplidar_composition\|rviz2\|ntfields_planner_node\|planner_server\|controller_server\|bt_navigator\|velocity_smoother\|scan_relay\|pose_init\|robot_state_publisher\|safety_shield\|ascamera_node\|pointcloud_restamper\|behavior_server\|static_transform_publisher" 2>/dev/null
sleep 1
# Force-free the ports too — a holder that doesn't show these process
# names in its own command line (or a leftover handle after the pkill
# above) survives untouched otherwise, causing serial_bridge/rplidar to
# see "device disconnected or multiple access on port" on the next launch.
fuser -k -9 /dev/esp32 /dev/lidar 2>/dev/null || true
sleep 1

# ── Step 1 — robot_state_publisher ──────────────────────────────────────
pause "Step 1 — robot_state_publisher"
ros2 launch argo_mini robot_state_publisher.launch.py &
RSP_PID=$!
sleep 2
check_process_alive $RSP_PID "robot_state_publisher"
check_topic "/robot_description" 10

# ── Step 2 — camera TF bridge ───────────────────────────────────────────
pause "Step 2 — camera TF bridge"
ros2 run tf2_ros static_transform_publisher \
  --x 0.2575 --y 0.0 --z 0.170 --roll 0.0 --pitch 0.0 --yaw 0.0 \
  --frame-id base_link --child-frame-id ascamera_hp60c_color_0 &
TF_PID=$!
sleep 1
check_process_alive $TF_PID "camera TF bridge"

# ── Step 3 — serial_bridge ──────────────────────────────────────────────
pause "Step 3 — serial_bridge (esp32)"
ros2 run argo_mini serial_bridge --ros-args \
  -p port:=/dev/esp32 -p baud:=115200 -p left_tick_scale:=0.66 &
SERIAL_PID=$!
sleep 2
check_process_alive $SERIAL_PID "serial_bridge"
check_topic "/odom" 10
check_topic "/wheel_speeds" 5

# ── Step 4 — rplidar ─────────────────────────────────────────────────────
pause "Step 4 — rplidar"
ros2 run rplidar_ros rplidar_composition --ros-args \
  -p serial_port:=/dev/lidar -p serial_baudrate:=115200 \
  -p frame_id:=lidar_link -p angle_compensate:=true -p scan_mode:=Boost &
LIDAR_PID=$!
sleep 2
check_process_alive $LIDAR_PID "rplidar"
check_topic "/scan" 10

# ── Step 5 — scan_relay ──────────────────────────────────────────────────
pause "Step 5 — scan_relay"
ros2 run argo_mini scan_relay &
RELAY_PID=$!
sleep 1
check_process_alive $RELAY_PID "scan_relay"
check_topic "/scan_corrected" 10

# ── Step 6 — slam_toolbox localization ──────────────────────────────────
pause "Step 6 — slam_toolbox localization"
ros2 run slam_toolbox localization_slam_toolbox_node --ros-args \
  --params-file "$SLAM_CONFIG" -p map_file_name:="$MAP_BASE" &
SLAM_PID=$!
check_process_alive $SLAM_PID "slam_toolbox"
check_topic "/map" 40

# ── Step 6.5 — pose_init ─────────────────────────────────────────────────
pause "Step 6.5 — pose_init"
ros2 run argo_mini pose_init --ros-args || fail_stop "pose_init exited non-zero"
echo "-- OK: pose_init ran to completion."

# ── Step 7 — NTFields planner ────────────────────────────────────────────
pause "Step 7 — NTFields planner (ntfields_planner_node)"
ros2 run argo_mini ntfields_planner_node --ros-args \
  --params-file "$NTFIELDS_CONFIG" -p model_path:="$NTFIELDS_MODEL" &
NTFIELDS_PID=$!
sleep 2
check_process_alive $NTFIELDS_PID "ntfields_planner_node"
pause "  now configure+activate /planner_server (watch PID $NTFIELDS_PID's output above)"
ros2 lifecycle set /planner_server configure
ros2 lifecycle set /planner_server activate
check_topic "/compute_path_to_pose/_action/status" 50

# ── Step 8 — controller_server ───────────────────────────────────────────
pause "Step 8 — controller_server"
ros2 run nav2_controller controller_server --ros-args \
  --params-file "$NAV_CONFIG" -r cmd_vel:=/cmd_vel_raw &
CTRL_PID=$!
sleep 2
check_process_alive $CTRL_PID "controller_server"
pause "  now configure+activate /controller_server"
ros2 lifecycle set /controller_server configure
ros2 lifecycle set /controller_server activate
check_lifecycle_active /controller_server 30
check_topic "local_costmap/costmap_raw" 15

# ── Step 9 — velocity_smoother ───────────────────────────────────────────
pause "Step 9 — velocity_smoother"
ros2 run nav2_velocity_smoother velocity_smoother --ros-args \
  --params-file "$NAV_CONFIG" \
  -r cmd_vel:=/cmd_vel_raw -r cmd_vel_smoothed:=/cmd_vel_smoothed &
SMOOTH_PID=$!
sleep 2
check_process_alive $SMOOTH_PID "velocity_smoother"
pause "  now configure+activate /velocity_smoother"
ros2 lifecycle set /velocity_smoother configure
ros2 lifecycle set /velocity_smoother activate
check_lifecycle_active /velocity_smoother 25

# ── Step 10 — behavior_server ────────────────────────────────────────────
pause "Step 10 — behavior_server"
ros2 run nav2_behaviors behavior_server --ros-args \
  --params-file "$NAV_CONFIG" -r cmd_vel:=/cmd_vel_raw &
BEHAVIOR_PID=$!
sleep 2
check_process_alive $BEHAVIOR_PID "behavior_server"
pause "  now configure+activate /behavior_server"
ros2 lifecycle set /behavior_server configure
ros2 lifecycle set /behavior_server activate
check_lifecycle_active /behavior_server 25
check_topic "/follow_path/_action/status" 15
check_topic "/backup/_action/status" 15

# ── Step 11 — bt_navigator ───────────────────────────────────────────────
pause "Step 11 — bt_navigator (with explicit BT xml override)"
ros2 run nav2_bt_navigator bt_navigator --ros-args --params-file "$NAV_CONFIG" \
  -p default_nav_to_pose_bt_xml:="$BT_XML" \
  -p default_nav_through_poses_bt_xml:="$BT_XML" &
BT_PID=$!
sleep 2
check_process_alive $BT_PID "bt_navigator"
pause "  now configure+activate /bt_navigator"
ros2 lifecycle set /bt_navigator configure
ros2 lifecycle set /bt_navigator activate
check_lifecycle_active /bt_navigator 30

echo
echo "==================================================================="
echo " ALL CORE STEPS PASSED — nav2 core (1-11) is confirmed working."
echo " Camera/restamper/safety_shield/RViz deliberately left out here;"
echo " once this is solid, switch to start_ntfields_nav_fast.sh for the"
echo " full automated run including those."
echo "==================================================================="
echo " Cleanup: pkill -9 -f 'slam_toolbox|serial_bridge|rplidar_composition|ntfields_planner_node|controller_server|velocity_smoother|behavior_server|bt_navigator|robot_state_publisher|static_transform_publisher|scan_relay'"
