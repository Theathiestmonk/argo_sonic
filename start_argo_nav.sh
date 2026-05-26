#!/bin/bash
# Argo Mini — full nav stack with depth camera safety shield
#
# cmd_vel pipeline:
#   controller_server → /cmd_vel_nav
#   velocity_smoother → /cmd_vel_nav → /cmd_vel_smoothed
#   depth_safety_shield → /cmd_vel_smoothed → /cmd_vel
#   serial_bridge → /cmd_vel → ESP32
#
# Usage:
#   ./start_argo_nav.sh           # with camera + RViz
#   ./start_argo_nav.sh --no-cam  # skip camera (lidar-only obstacle avoidance)

set -e  # stop on any unexpected error

NO_CAM=false
for arg in "$@"; do
  [[ "$arg" == "--no-cam" ]] && NO_CAM=true
done

# ── environment ────────────────────────────────────────────────────────────
source /opt/ros/humble/setup.bash
source ~/argo_mini_ws/install/setup.bash

CAMERA_SDK_PATH=~/EaiCameraSdk_v1.2.28.20241015/demo/linux_ros/ros2

# Fix camera SDK shared library path.
# If you see "libAngstrongCameraSdk.so: cannot open shared object file",
# run:  find ~/EaiCameraSdk* -name "libAngstrongCameraSdk.so"
# and update the path below to the directory containing that file.
export LD_LIBRARY_PATH=$LD_LIBRARY_PATH:$CAMERA_SDK_PATH/ascamera/libs/lib/aarch64-linux-gnu

NAV_CONFIG=~/argo_mini_ws/install/argo_mini/share/argo_mini/config/nav2.yaml
MAP_FILE=~/argo_mini_ws/src/argo_mini/maps/indoor_map.yaml

# ── cleanup previous run ───────────────────────────────────────────────────
echo "[argo] Killing previous processes..."
pkill -9 -f slam_toolbox       2>/dev/null || true
pkill -9 -f serial_bridge      2>/dev/null || true
pkill -9 -f rplidar            2>/dev/null || true
pkill -9 -f rviz2              2>/dev/null || true
pkill -9 -f amcl               2>/dev/null || true
pkill -9 -f planner_server     2>/dev/null || true
pkill -9 -f controller_server  2>/dev/null || true
pkill -9 -f bt_navigator       2>/dev/null || true
pkill -9 -f velocity_smoother  2>/dev/null || true
pkill -9 -f scan_relay         2>/dev/null || true
pkill -9 -f robot_state_pub    2>/dev/null || true
pkill -9 -f depth_safety_shield 2>/dev/null || true
pkill -9 -f ascamera           2>/dev/null || true
sleep 3

sudo chmod 666 /dev/ttyUSB0 /dev/ttyUSB1 2>/dev/null || true

# ── 1. Robot state publisher (URDF → TF) ──────────────────────────────────
echo "[argo] Starting robot_state_publisher..."
ros2 launch argo_mini robot_state_publisher.launch.py &
RSP_PID=$!
sleep 2

# ── 2. Serial bridge (ESP32 motors + odometry) ────────────────────────────
echo "[argo] Starting serial_bridge..."
ros2 run argo_mini serial_bridge --ros-args \
  -p port:=/dev/ttyUSB1 -p baud:=115200 &
SERIAL_PID=$!
sleep 3

# ── 3. RPLidar A1 ─────────────────────────────────────────────────────────
echo "[argo] Starting rplidar..."
ros2 run rplidar_ros rplidar_composition --ros-args \
  -p serial_port:=/dev/ttyUSB0 \
  -p serial_baudrate:=115200 \
  -p frame_id:=laser \
  -p angle_compensate:=true \
  -p scan_mode:=Standard &
LIDAR_PID=$!
sleep 3

# ── 4. Scan relay (timestamp correction) ──────────────────────────────────
echo "[argo] Starting scan_relay..."
ros2 run argo_mini scan_relay &
RELAY_PID=$!
sleep 2

# ── 5. Static TF: map → odom (initial pose before AMCL takes over) ────────
echo "[argo] Starting static TFs..."
ros2 run tf2_ros static_transform_publisher \
  --x 0.0 --y 0.0 --z 0.0 \
  --roll 0.0 --pitch 0.0 --yaw 0.0 \
  --frame-id map --child-frame-id odom &
TF_PID=$!
sleep 1

# ── 6. Map server ─────────────────────────────────────────────────────────
echo "[argo] Starting map_server..."
ros2 run nav2_map_server map_server --ros-args \
  -p yaml_filename:=$MAP_FILE -p use_sim_time:=false &
MAP_PID=$!
sleep 3
ros2 lifecycle set /map_server configure && sleep 1
ros2 lifecycle set /map_server activate
sleep 1

# ── 7. AMCL ───────────────────────────────────────────────────────────────
echo "[argo] Starting AMCL..."
ros2 run nav2_amcl amcl --ros-args \
  --params-file $NAV_CONFIG \
  -p base_frame_id:=base_link \
  -p odom_frame_id:=odom \
  -p global_frame_id:=map \
  -p scan_topic:=/scan_corrected &
AMCL_PID=$!
sleep 3
ros2 lifecycle set /amcl configure && sleep 1
ros2 lifecycle set /amcl activate
sleep 1

# ── 8. Planner server ─────────────────────────────────────────────────────
echo "[argo] Starting planner_server..."
ros2 run nav2_planner planner_server --ros-args --params-file $NAV_CONFIG &
PLANNER_PID=$!
sleep 3
ros2 lifecycle set /planner_server configure && sleep 1
ros2 lifecycle set /planner_server activate
sleep 1

# ── 9. Controller server → /cmd_vel_nav ───────────────────────────────────
# Remapped so velocity_smoother sits between controller and motors.
echo "[argo] Starting controller_server..."
ros2 run nav2_controller controller_server --ros-args \
  --params-file $NAV_CONFIG \
  -r cmd_vel:=/cmd_vel_nav &
CONTROLLER_PID=$!
sleep 3
ros2 lifecycle set /controller_server configure && sleep 1
ros2 lifecycle set /controller_server activate
sleep 1

# ── 10. Velocity smoother  /cmd_vel_nav → /cmd_vel_smoothed ───────────────
echo "[argo] Starting velocity_smoother..."
ros2 run nav2_velocity_smoother velocity_smoother --ros-args \
  --params-file $NAV_CONFIG \
  -r cmd_vel:=/cmd_vel_nav \
  -r cmd_vel_smoothed:=/cmd_vel_smoothed &
SMOOTHER_PID=$!
sleep 3
ros2 lifecycle set /velocity_smoother configure && sleep 1
ros2 lifecycle set /velocity_smoother activate
sleep 1

# ── 11. BT Navigator ──────────────────────────────────────────────────────
echo "[argo] Starting bt_navigator..."
ros2 run nav2_bt_navigator bt_navigator --ros-args --params-file $NAV_CONFIG &
BT_PID=$!
sleep 3
ros2 lifecycle set /bt_navigator configure && sleep 2
ros2 lifecycle set /bt_navigator activate
sleep 1

# ── 12. HP60C Depth camera (optional) ────────────────────────────────────
CAM_PID=""
CAM_TF_PID=""
if [ "$NO_CAM" = false ]; then
  echo "[argo] Starting HP60C camera..."
  # Must cd into the SDK directory — the node looks for
  # ./ascamera/configurationfiles relative to its working directory.
  (
    cd $CAMERA_SDK_PATH
    source install/setup.bash
    ros2 launch ascamera hp60c.launch.py
  ) &
  CAM_PID=$!
  sleep 5

  # Bridge the SDK's frame_id to our URDF camera frame.
  # The HP60C SDK publishes depth0/points with frame_id: ascamera_hp60c_color_0
  # Our URDF defines camera_depth_optical_frame at the same physical location.
  ros2 run tf2_ros static_transform_publisher \
    --x 0.0 --y 0.0 --z 0.0 \
    --roll 0.0 --pitch 0.0 --yaw 0.0 \
    --frame-id camera_depth_optical_frame \
    --child-frame-id ascamera_hp60c_color_0 &
  CAM_TF_PID=$!
  sleep 1
else
  echo "[argo] Camera skipped (--no-cam)"
fi

# ── 13. Depth safety shield  /cmd_vel_smoothed → /cmd_vel ─────────────────
# Runs in STALE (pass-through) mode when camera is off, so navigation still works.
echo "[argo] Starting depth_safety_shield..."
ros2 run argo_mini depth_safety_shield --ros-args \
  -p stop_distance:=0.35 \
  -p slow_distance:=0.65 \
  -p slow_factor:=0.40 \
  -p lateral_margin:=0.28 \
  -p min_obstacle_height:=0.05 \
  -p max_obstacle_height:=1.60 \
  -p depth_timeout:=3.0 \
  -p downsample_stride:=4 \
  -p input_topic:=/cmd_vel_smoothed \
  -p output_topic:=/cmd_vel \
  -p depth_topic:=/ascamera_hp60c/camera_publisher/depth0/points &
SHIELD_PID=$!
sleep 2

# ── 14. RViz ──────────────────────────────────────────────────────────────
echo "[argo] Starting RViz..."
export DISPLAY=:1
rviz2 &
RVIZ_PID=$!

echo ""
echo "========================================="
echo "  ARGO MINI — NAV2 + DEPTH SAFETY"
echo "========================================="
echo "  cmd_vel pipeline:"
echo "    controller → /cmd_vel_nav"
echo "    smoother   → /cmd_vel_smoothed"
echo "    shield     → /cmd_vel  (STOP<0.35m, SLOW<0.65m)"
echo ""
echo "  Camera: $( [ '$NO_CAM' = false ] && echo 'enabled' || echo 'disabled' )"
echo "  Press Ctrl+C to stop all"
echo "========================================="
echo ""

trap '
  echo "[argo] Shutting down..."
  kill $RSP_PID $SERIAL_PID $LIDAR_PID $RELAY_PID $TF_PID \
       $MAP_PID $AMCL_PID $PLANNER_PID $CONTROLLER_PID \
       $SMOOTHER_PID $BT_PID $SHIELD_PID $RVIZ_PID \
       ${CAM_PID:-} ${CAM_TF_PID:-} 2>/dev/null || true
  sleep 2
  pkill -9 -f ros2 2>/dev/null || true
  exit 0
' INT TERM

wait $RVIZ_PID
