#!/bin/bash
# Argo Mini — SLAM mapping session
#
# Usage:
#   ./start_slam.sh              # full SLAM + RViz
#   ./start_slam.sh --no-rviz   # headless
#
# Save map when done:
#   ros2 service call /slam_toolbox/serialize_map \
#     slam_toolbox/srv/SerializePoseGraph \
#     "{filename: '/home/argo/maps/indoor_map'}"

NO_RVIZ=false
for arg in "$@"; do
  [[ "$arg" == "--no-rviz" ]] && NO_RVIZ=true
done

# ── environment ────────────────────────────────────────────────────────────
source /opt/ros/humble/setup.bash
source ~/argo_mini_ws/install/setup.bash

SLAM_CONFIG=~/argo_mini_ws/install/argo_mini/share/argo_mini/config/slam_mapping.yaml

# ── USB permissions ────────────────────────────────────────────────────────
chmod 666 /dev/ttyUSB0 /dev/ttyUSB1 2>/dev/null || \
  sudo chmod 666 /dev/ttyUSB0 /dev/ttyUSB1 2>/dev/null || true

# ── kill previous run ──────────────────────────────────────────────────────
echo "[slam] Killing previous processes..."
for proc in slam_toolbox serial_bridge rplidar_composition rviz2 \
            robot_state_publisher scan_relay; do
  pkill -9 -f "$proc" 2>/dev/null || true
done
sleep 3

# ── 1. Robot state publisher ──────────────────────────────────────────────
echo "[slam] Starting robot_state_publisher..."
ros2 launch argo_mini robot_state_publisher.launch.py &
RSP_PID=$!
sleep 2

# ── 2. Serial bridge ──────────────────────────────────────────────────────
# left_tick_scale=2.1714: right wheel ticks 2.17x faster at same DAC
# fixed_dac=106: constant DAC → constant tick rate → cleaner odom
echo "[slam] Starting serial_bridge..."
ros2 run argo_mini serial_bridge --ros-args \
  -p port:=/dev/ttyUSB1 \
  -p baud:=115200 \
  -p left_tick_scale:=2.1714 \
  -p fixed_dac:=112 &
SERIAL_PID=$!
sleep 3

# ── 3. RPLidar A1 ─────────────────────────────────────────────────────────
echo "[slam] Starting rplidar..."
ros2 run rplidar_ros rplidar_composition --ros-args \
  -p serial_port:=/dev/ttyUSB0 \
  -p serial_baudrate:=115200 \
  -p frame_id:=lidar_link \
  -p angle_compensate:=true \
  -p scan_mode:=Standard &
LIDAR_PID=$!
sleep 3

# ── 4. Scan relay (timestamp correction) ──────────────────────────────────
echo "[slam] Starting scan_relay..."
ros2 run argo_mini scan_relay &
RELAY_PID=$!
sleep 2

# ── 5. SLAM Toolbox (async mapping) ───────────────────────────────────────
echo "[slam] Starting slam_toolbox (mapping mode)..."
ros2 run slam_toolbox async_slam_toolbox_node --ros-args \
  --params-file "$SLAM_CONFIG" &
SLAM_PID=$!
sleep 3

# ── 6. RViz (optional) ────────────────────────────────────────────────────
RVIZ_PID=""
if [ "$NO_RVIZ" = false ]; then
  echo "[slam] Starting RViz..."
  export DISPLAY=:1
  rviz2 &
  RVIZ_PID=$!
fi

echo ""
echo "========================================="
echo "  ARGO MINI — SLAM MAPPING"
echo "========================================="
echo "  Teleop: ros2 run argo_mini slam_teleop"
echo ""
echo "  Save map when done:"
echo "  mkdir -p ~/maps"
echo "  ros2 service call /slam_toolbox/serialize_map \\"
echo "    slam_toolbox/srv/SerializePoseGraph \\"
echo "    \"{filename: '/home/argo/maps/indoor_map'}\""
echo ""
echo "  Press Ctrl+C to stop"
echo "========================================="
echo ""

trap '
  echo "[slam] Shutting down..."
  kill $RSP_PID $SERIAL_PID $LIDAR_PID $RELAY_PID $SLAM_PID \
       ${RVIZ_PID:-} 2>/dev/null || true
  sleep 2
  pkill -9 -f "slam_toolbox\|serial_bridge\|rplidar\|scan_relay" 2>/dev/null || true
  exit 0
' INT TERM

if [ "$NO_RVIZ" = false ] && [ -n "$RVIZ_PID" ]; then
  wait $RVIZ_PID
else
  wait $SLAM_PID
fi
