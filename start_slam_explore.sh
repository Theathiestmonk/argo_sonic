#!/bin/bash
# start_slam_explore.sh – Autonomous frontier exploration with live SLAM mapping.
#
# Stack launched in order:
#   1.  robot_state_publisher
#   2.  serial_bridge         (odometry + motor control)
#   3.  rplidar               (LiDAR scan)
#   4.  scan_relay            (timestamp fix → /scan_corrected)
#   5.  slam_toolbox          (async mapping → /map + map→odom TF)
#   6.  rviz2                 (optional visualisation)
#   ── sleep 8s ─────────────────────── (SLAM establishes map frame)
#   7.  behavior_server       (nav2.yaml + exploration_nav2.yaml → wait-only)
#   8.  planner_server        (exploration_nav2.yaml → NavFn allow_unknown)
#   9.  controller_server     (nav2.yaml + exploration_nav2.yaml → MPPI tuned)
#   10. velocity_smoother     (/cmd_vel_raw → /cmd_vel_smoothed)
#   11. safety_shield         (/cmd_vel_smoothed → /cmd_vel, lidar+US 10cm last-resort)
#   12. bt_navigator          (exploration BT: no Spin/BackUp, costmap-clear recovery)
#   13. frontier_explorer     (exploration brain)
#
# Usage:
#   ./start_slam_explore.sh
#
# Save the map when done:
#   ros2 service call /slam_toolbox/save_map \
#     slam_toolbox/srv/SaveMap "{name: {data: '/home/argo/maps/explored_map'}}"

set -euo pipefail

# ── Environment ───────────────────────────────────────────────────────────────
source /opt/ros/humble/setup.bash
source ~/argo_sonic/install/setup.bash

SHARE=~/argo_sonic/install/argo_mini/share/argo_mini
NAV_CONFIG=$SHARE/config/nav2.yaml
EXPLORE_CONFIG=$SHARE/config/exploration_nav2.yaml
SLAM_CONFIG=$SHARE/config/slam_mapping.yaml
FRONTIER_NODE=~/argo_sonic/src/argo_mini/argo_mini/frontier_explorer.py

# ── USB permissions ───────────────────────────────────────────────────────────
chmod 666 /dev/ttyUSB0 /dev/ttyUSB1 2>/dev/null || \
  sudo chmod 666 /dev/ttyUSB0 /dev/ttyUSB1 2>/dev/null || true

# ── Kill any previous run ─────────────────────────────────────────────────────
echo "[explore] Killing previous processes..."
for proc in slam_toolbox serial_bridge rplidar_composition rviz2 \
            planner_server controller_server behavior_server \
            bt_navigator velocity_smoother scan_relay \
            robot_state_publisher safety_shield frontier_explorer; do
  pkill -9 -f "$proc" 2>/dev/null || true
done
sleep 5

# ── Helpers ───────────────────────────────────────────────────────────────────
lc_node() {
  local node=$1
  echo "[explore]   configure $node..."
  ros2 lifecycle set "$node" configure 2>&1 | tail -1
  sleep 4
  echo "[explore]   activate  $node..."
  ros2 lifecycle set "$node" activate  2>&1 | tail -1
  sleep 4
}

wait_for_topic() {
  local topic=$1
  local timeout=${2:-30}
  local start
  start=$(date +%s)
  echo "[explore] Waiting for topic $topic (timeout: ${timeout}s)..."
  while true; do
    if ros2 topic list 2>/dev/null | grep -q "^${topic}$"; then
      echo "[explore] ✓ $topic"
      return 0
    fi
    local elapsed=$(( $(date +%s) - start ))
    if [ "$elapsed" -ge "$timeout" ]; then
      echo "[explore] ✗ $topic not available after ${timeout}s – continuing anyway"
      return 1
    fi
    sleep 1
  done
}

wait_for_action() {
  local action=$1
  local timeout=${2:-30}
  local start
  start=$(date +%s)
  echo "[explore] Waiting for action $action (timeout: ${timeout}s)..."
  while true; do
    if ros2 action list 2>/dev/null | grep -q "^${action}$"; then
      echo "[explore] ✓ $action"
      return 0
    fi
    local elapsed=$(( $(date +%s) - start ))
    if [ "$elapsed" -ge "$timeout" ]; then
      echo "[explore] ✗ $action not available after ${timeout}s – continuing anyway"
      return 1
    fi
    sleep 1
  done
}

# ── 1. Robot state publisher ──────────────────────────────────────────────────
echo "[explore] 1. robot_state_publisher..."
ros2 launch argo_mini robot_state_publisher.launch.py &
RSP_PID=$!
sleep 5

# ── 2. Serial bridge (odometry + motor control) ───────────────────────────────
echo "[explore] 2. serial_bridge..."
ros2 run argo_mini serial_bridge --ros-args \
  -p port:=/dev/ttyUSB1 \
  -p baud:=115200 \
  -p left_tick_scale:=0.66 &
SERIAL_PID=$!
sleep 5

# ── 3. RPLidar A1 ─────────────────────────────────────────────────────────────
echo "[explore] 3. rplidar..."
ros2 run rplidar_ros rplidar_composition --ros-args \
  -p serial_port:=/dev/ttyUSB0 \
  -p serial_baudrate:=115200 \
  -p frame_id:=lidar_link \
  -p angle_compensate:=true \
  -p scan_mode:=Boost &
LIDAR_PID=$!
sleep 5

# ── 4. Scan relay (timestamp fix) ─────────────────────────────────────────────
echo "[explore] 4. scan_relay..."
ros2 run argo_mini scan_relay &
RELAY_PID=$!
sleep 4

# ── 5. SLAM Toolbox (async mapping – builds /map and map→odom TF live) ────────
echo "[explore] 5. slam_toolbox (async mapping)..."
ros2 run slam_toolbox async_slam_toolbox_node --ros-args \
  --params-file "$SLAM_CONFIG" &
SLAM_PID=$!

echo "[explore] Waiting 8s for SLAM to establish map frame..."
sleep 8

# ── 6. RViz (optional – comment out if running headless) ──────────────────────
echo "[explore] 6. rviz2..."
export DISPLAY="${DISPLAY:-:1}"
rviz2 &
RVIZ_PID=$!

# ── 7. Behavior server (wait-only: no physical recovery motion) ───────────────
echo "[explore] 7. behavior_server..."
ros2 run nav2_behaviors behavior_server --ros-args \
  --params-file "$NAV_CONFIG" \
  --params-file "$EXPLORE_CONFIG" \
  -r cmd_vel:=/cmd_vel_raw &
BEHAVIOR_PID=$!
sleep 7

# Costmap topics appear after controller/planner activate; wait is best-effort.
wait_for_topic "local_costmap/costmap_raw" 15 || true
wait_for_topic "global_costmap/costmap_raw" 15 || true
lc_node /behavior_server

# ── 8. Planner server (NavFn + global costmap with track_unknown_space) ───────
echo "[explore] 8. planner_server (NavFn allow_unknown)..."
ros2 run nav2_planner planner_server --ros-args \
  --params-file "$NAV_CONFIG" \
  --params-file "$EXPLORE_CONFIG" &
PLANNER_PID=$!
sleep 5
lc_node /planner_server

# ── 9. Controller server (/cmd_vel_raw output) ────────────────────────────────
echo "[explore] 9. controller_server..."
ros2 run nav2_controller controller_server --ros-args \
  --params-file "$NAV_CONFIG" \
  --params-file "$EXPLORE_CONFIG" \
  -r cmd_vel:=/cmd_vel_raw &
CONTROLLER_PID=$!
sleep 5
lc_node /controller_server

# ── 10. Velocity smoother (/cmd_vel_raw → /cmd_vel_smoothed) ─────────────────
echo "[explore] 10. velocity_smoother..."
ros2 run nav2_velocity_smoother velocity_smoother --ros-args \
  --params-file "$NAV_CONFIG" \
  -r cmd_vel:=/cmd_vel_raw \
  -r cmd_vel_smoothed:=/cmd_vel_smoothed &
SMOOTHER_PID=$!
sleep 5
lc_node /velocity_smoother

# ── 11. Safety shield (/cmd_vel_smoothed → /cmd_vel, 10 cm last-resort) ───────
echo "[explore] 11. safety_shield..."
ros2 run argo_mini safety_shield &
SHIELD_PID=$!
sleep 3

wait_for_topic "/cmd_vel_smoothed" 10 || {
  echo "[explore] ✗ velocity_smoother not publishing – aborting"
  exit 1
}

# Wait for action servers before BT navigator loads plugins.
wait_for_action "/compute_path_to_pose" 30 || true
wait_for_action "/follow_path"          30 || true
wait_for_action "/wait"                 15 || true

# ── 12. BT Navigator (exploration BT via exploration_nav2.yaml) ───────────────
echo "[explore] 12. bt_navigator..."
ros2 run nav2_bt_navigator bt_navigator --ros-args \
  --params-file "$NAV_CONFIG" \
  --params-file "$EXPLORE_CONFIG" &
BT_PID=$!
sleep 7
lc_node /bt_navigator

# ── 13. Frontier explorer ─────────────────────────────────────────────────────
wait_for_action "/navigate_to_pose" 30 || {
  echo "[explore] ✗ navigate_to_pose action not available – aborting"
  exit 1
}

echo "[explore] 13. frontier_explorer..."
# Runs directly so setup.py entry-point is not required.
# After adding  frontier_explorer=argo_mini.frontier_explorer:main
# to setup.py and rebuilding you can replace this with:
#   ros2 run argo_mini frontier_explorer &
python3 "$FRONTIER_NODE" &
FRONTIER_PID=$!

echo ""
echo "================================================"
echo "  ARGO MINI – FRONTIER EXPLORATION"
echo "================================================"
echo "  SLAM:     async mapping  → /map"
echo "  Planner:  NavFn          (allow_unknown: true)"
echo "  Recovery: costmap-clear  (zero robot motion)"
echo "  Shield:   safety_shield  (lidar + US, 10 cm)"
echo ""
echo "  Save map when done:"
echo "  ros2 service call /slam_toolbox/save_map \\"
echo "    slam_toolbox/srv/SaveMap \\"
echo "    \"{name: {data: '/home/argo/maps/explored_map'}}\""
echo ""
echo "  Press Ctrl+C to stop all nodes."
echo "================================================"
echo ""

trap '
  echo "[explore] Shutting down..."
  kill "$RSP_PID" "$SERIAL_PID" "$LIDAR_PID" "$RELAY_PID" \
       "$SLAM_PID" "$BEHAVIOR_PID" "$PLANNER_PID" "$CONTROLLER_PID" \
       "$SMOOTHER_PID" "$SHIELD_PID" "$BT_PID" "$FRONTIER_PID" \
       "${RVIZ_PID:-}" 2>/dev/null || true
  sleep 4
  pkill -9 -f ros2 2>/dev/null || true
  exit 0
' INT TERM

wait "$RVIZ_PID"
