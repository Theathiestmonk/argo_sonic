#!/bin/bash
# Wrapper so systemd can launch rosbridge with the full ROS environment.
# Full paths used throughout — systemd does not inherit the user's PATH.

source /opt/ros/humble/setup.bash

# Fast-DDS (ROS2 Humble's default RMW) is known to be less reliable under
# discovery/service-call load than Cyclone DDS, especially with this many
# concurrent nodes on Jetson-class ARM hardware — seen firsthand as
# intermittent lifecycle-transition timeouts during nav stack startup.
# Every ROS2 process on this machine must agree on the same RMW or they
# simply can't discover each other at all, so this has to be set here too,
# not just in the nav/SLAM scripts.
export RMW_IMPLEMENTATION=rmw_cyclonedds_cpp

# Also source the argo workspace if present (finds argo_mini packages)
for ws in \
    "$HOME/my_project/argo_sonic/install/setup.bash" \
    "$HOME/dhruvil/argo_sonic/install/setup.bash" \
    "$HOME/argo_mini_ws/install/setup.bash"
do
    [ -f "$ws" ] && source "$ws" && break
done

exec /opt/ros/humble/bin/ros2 launch rosbridge_server rosbridge_websocket_launch.xml
