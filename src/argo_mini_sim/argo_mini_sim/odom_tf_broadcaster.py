#!/usr/bin/env python3
"""
Republishes /odom as the odom->base_footprint TF transform, copying the
Odometry message's own header stamp.

Why this exists: ros_gz_bridge's Pose_V -> tf2_msgs/TFMessage conversion
(used to bridge the DiffDrive plugin's native <tf_topic>) intermittently
stamps transforms with wall-clock time instead of simulated time on this
Gazebo/ros_gz_bridge version, corrupting the TF buffer and making every
consumer (SLAM, costmaps, planner) see constant TF_OLD_DATA errors.
nav_msgs/Odometry has a proper header and bridges with correct sim-time
stamps, so this node uses that as the single source of truth for the
dynamic odom->base_footprint transform instead of the buggy /tf bridge.
"""

import rclpy
from geometry_msgs.msg import TransformStamped
from nav_msgs.msg import Odometry
from rclpy.node import Node
from tf2_ros import TransformBroadcaster


class OdomTfBroadcaster(Node):

    def __init__(self):
        super().__init__('odom_tf_broadcaster')
        self._broadcaster = TransformBroadcaster(self)
        self.create_subscription(Odometry, 'odom', self._on_odom, 50)

    def _on_odom(self, msg: Odometry):
        t = TransformStamped()
        t.header.stamp = msg.header.stamp
        t.header.frame_id = msg.header.frame_id
        t.child_frame_id = msg.child_frame_id
        t.transform.translation.x = msg.pose.pose.position.x
        t.transform.translation.y = msg.pose.pose.position.y
        t.transform.translation.z = msg.pose.pose.position.z
        t.transform.rotation = msg.pose.pose.orientation
        self._broadcaster.sendTransform(t)


def main():
    rclpy.init()
    node = OdomTfBroadcaster()
    try:
        rclpy.spin(node)
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    main()
