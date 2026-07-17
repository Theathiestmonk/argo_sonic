#!/bin/bash
# Wrapper so systemd can launch rosbridge with the full ROS environment.
# Full paths used throughout — systemd does not inherit the user's PATH.

source /opt/ros/humble/setup.bash

# Also source the argo workspace if present (finds argo_mini packages)
for ws in \
    "$HOME/my_project/argo_sonic/install/setup.bash" \
    "$HOME/dhruvil/argo_sonic/install/setup.bash" \
    "$HOME/argo_mini_ws/install/setup.bash"
do
    [ -f "$ws" ] && source "$ws" && break
done

exec /opt/ros/humble/bin/ros2 launch rosbridge_server rosbridge_websocket_launch.xml
