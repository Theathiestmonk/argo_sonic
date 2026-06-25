#!/bin/bash
# Argo Mini - Navigation with Active Neural Fields (NTFields) + SLAM Toolbox
#
# Usage:
#   ./start_neural_planner.sh                          # with camera + RViz
#   ./start_neural_planner.sh --no-cam                 # lidar-only

NO_CAM=false
MAP_BASE=~/argo_mini_ws/src/argo_mini/maps/office_map

for arg in "$@"; do
  [[ "$arg" == "--no-cam" ]] && NO_CAM=true
  [[ "$arg" == "--map" ]]    && { shift; MAP_BASE="$1"; }
done
MAP_BASE="${MAP_BASE/#\~/$HOME}"

# Environment Setup
source /opt/ros/humble/setup.bash
source ~/argo_mini_ws/install/setup.bash

CAMERA_SDK_PATH=~/EaiCameraSdk_v1.2.28.20241015/demo/linux_ros/ros2
export LD_LIBRARY_PATH=$LD_LIBRARY_PATH:$CAMERA_SDK_PATH/ascamera/libs/lib/aarch64-linux-gnu

SLAM_CONFIG=~/argo_mini_ws/install/argo_mini/share/argo_mini/config/slam_toolbox.yaml

# USB Permissions
chmod 666 /dev/ttyUSB0 /dev/ttyUSB1 2>/dev/null || \
  sudo chmod 666 /dev/ttyUSB0 /dev/ttyUSB1 2>/dev/null || true

# Kill any previous run planning/driver processes (excluding the external safety shield)
echo "[argo-neural] Killing previous local planning and driver processes..."
for proc in slam_toolbox serial_bridge rplidar_composition rviz2 \
            scan_relay robot_state_publisher ascamera_node active_planner_node; do
  pkill -9 -f "$proc" 2>/dev/null || true
done
sleep 3

# Topic checker
wait_for_topic() {
  local topic=$1
  local timeout=${2:-30}
  local start=$(date +%s)

  echo "[argo-neural] Waiting for topic $topic (timeout: ${timeout}s)..."
  while true; do
    if ros2 topic list 2>/dev/null | grep -q "^${topic}$"; then
      echo "[argo-neural] Topic $topic is available"
      return 0
    fi

    local elapsed=$(($(date +%s) - start))
    if [ $elapsed -ge $timeout ]; then
      echo "[argo-neural] ERROR: Topic $topic not available after ${timeout}s"
      return 1
    fi
    sleep 1
  done
}

# Process checker
check_process() {
  local pid=$1
  local name=$2
  if ! kill -0 "$pid" 2>/dev/null; then
    echo "[argo-neural] ERROR: Process $name (PID $pid) has crashed!"
    return 1
  fi
  return 0
}

# 1. Robot State Publisher (URDF)
echo "[argo-neural] Starting robot_state_publisher..."
ros2 launch argo_mini robot_state_publisher.launch.py &
RSP_PID=$!
sleep 3

# 2. Camera TF Bridges
echo "[argo-neural] Starting camera TF bridges..."
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
sleep 2

# 3. Serial Bridge (Hardware Interface)
echo "[argo-neural] Starting serial_bridge..."
ros2 run argo_mini serial_bridge --ros-args \
  -p port:=/dev/ttyUSB1 \
  -p baud:=115200 \
  -p left_tick_scale:=2.1714 &
SERIAL_PID=$!
sleep 4

# 4. RPLidar A1
echo "[argo-neural] Starting rplidar..."
ros2 run rplidar_ros rplidar_composition --ros-args \
  -p serial_port:=/dev/ttyUSB0 \
  -p serial_baudrate:=115200 \
  -p frame_id:=lidar_link \
  -p angle_compensate:=true \
  -p scan_mode:=Boost &
LIDAR_PID=$!
sleep 4

# 5. Scan Relay
echo "[argo-neural] Starting scan_relay..."
ros2 run argo_mini scan_relay &
RELAY_PID=$!
sleep 3

# 6. SLAM Toolbox - Localization Mode
echo "[argo-neural] Starting slam_toolbox localization (map: $MAP_BASE)..."
ros2 run slam_toolbox localization_slam_toolbox_node --ros-args \
  --params-file "$SLAM_CONFIG" \
  -p map_file_name:="$MAP_BASE" &
SLAM_PID=$!
sleep 5

# 7. Start the GPU-Accelerated Active Neural Planner Node
#
# TOPIC ROUTING NOTE:
# - If your safety shield script is running, we remap '/cmd_vel' to '/cmd_vel_smoothed' 
#   so the safety shield can intercept the messages:
#     ros2 run argo_neural_planner planner_node --ros-args -r /cmd_vel:=/cmd_vel_smoothed
#
# - If you are bypass-testing the safety shield and want to write directly to the motors:
#     ros2 run argo_neural_planner planner_node
#
echo "[argo-neural] Starting Active Neural Planner (NTFields)..."
ros2 run argo_neural_planner planner_node &
NEURAL_PLANNER_PID=$!
sleep 4

# Verify the neural planner process
if check_process $NEURAL_PLANNER_PID "planner_node"; then
  echo "[argo-neural] Neural Planner is ready"
else
  echo "[argo-neural] ERROR: Neural Planner failed to start"
  exit 1
fi

# 8. Depth Camera (Optional)
CAM_PID=""
if [ "$NO_CAM" = false ]; then
  echo "[argo-neural] Starting HP60C camera..."
  (
    cd $CAMERA_SDK_PATH
    source install/setup.bash
    export LD_LIBRARY_PATH=$LD_LIBRARY_PATH:$CAMERA_SDK_PATH/ascamera/libs/lib/aarch64-linux-gnu
    ros2 launch ascamera hp60c.launch.py 2>&1 | sed 's/^/[camera] /'
  ) &
  CAM_PID=$!

  # Wait for camera depth points
  if wait_for_topic "/ascamera_hp60c/camera_publisher/depth0/points" 15; then
    echo "[argo-neural] Camera ready"
  else
    echo "[argo-neural] WARNING: Camera depth point topic not available"
  fi
else
  echo "[argo-neural] Camera skipped (--no-cam)"
fi

# 9. RViz
echo "[argo-neural] Starting RViz..."
export DISPLAY=:1
rviz2 &
RVIZ_PID=$!

echo ""
echo "========================================================="
echo "  ARGO MINI - ACTIVE NEURAL TIME FIELD NAVIGATION (NTFields)"
echo "========================================================="
echo "  Map:      $MAP_BASE"
echo "  Camera:   $([ "$NO_CAM" = false ] && echo 'enabled' || echo 'disabled')"
echo "  Pipeline: NTFields -> /cmd_vel_smoothed (Intercepted by external safety shield)"
echo ""
echo "  Active ROS Topics:"
ros2 topic list 2>/dev/null | grep -E "(cmd_vel|depth|scan|odom|goal_pose)" | sed 's/^/    /'
echo ""
echo "  Use RViz 2D Goal Pose to send navigation commands."
echo "  Press Ctrl+C to stop all local nodes"
echo "========================================================="
echo ""

trap '
  echo "[argo-neural] Shutting down..."
  kill $RSP_PID $SERIAL_PID $LIDAR_PID $RELAY_PID \
       $SLAM_PID $NEURAL_PLANNER_PID $RVIZ_PID \
       ${CAM_PID:-} $CAM_TF_PID $CAM_TF2_PID 2>/dev/null || true
  sleep 2
  pkill -9 -f ros2 2>/dev/null || true
  exit 0
' INT TERM

wait $RVIZ_PID