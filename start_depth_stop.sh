#!/bin/bash
source /opt/ros/humble/setup.bash
source ~/argo_mini_ws/install/setup.bash
echo "[depth_stop] Starting depth obstacle brake..."
python3 "$(dirname "$0")/depth_stop.py"
