#!/usr/bin/env python3
"""
Depth Camera Safety Shield ? Argo Mini (Simplified)

Pipeline:
    Nav2 controller ? /cmd_vel_raw
    velocity_smoother ? /cmd_vel_raw ? /cmd_vel_smoothed
    [this node] ? /cmd_vel_smoothed ? /cmd_vel
    serial_bridge ? /cmd_vel ? ESP32

Behavior:
    OBSTACLE DETECTED ? Stop forward motion (allow rotation)
    NO OBSTACLE      ? Passthrough all commands
"""

import math

import rclpy
from geometry_msgs.msg import Twist
from rclpy.node import Node
from rclpy.qos import QoSPresetProfiles
from sensor_msgs.msg import PointCloud2
from sensor_msgs_py import point_cloud2 as pc2
from std_msgs.msg import String


class DepthSafetyShield(Node):

    def __init__(self):
        super().__init__('depth_safety_shield')

        # Simple obstacle detection parameters
        self.declare_parameter('stop_distance',    0.50)   # m: stop if obstacle closer than this
        self.declare_parameter('height_min',       0.15)   # m: ignore floor reflections
        self.declare_parameter('height_max',       1.80)   # m: ignore ceiling
        self.declare_parameter('forward_range',    1.50)   # m: max forward detection range
        self.declare_parameter('width_margin',     0.30)   # m: half-width safety corridor
        self.declare_parameter('input_topic',      '/cmd_vel_smoothed')
        self.declare_parameter('output_topic',     '/cmd_vel')
        self.declare_parameter('depth_topic',      '/ascamera_hp60c/camera_publisher/depth0/points')

        p = self.get_parameter
        self.stop_dist = p('stop_distance').value
        self.h_min = p('height_min').value
        self.h_max = p('height_max').value
        self.fwd_range = p('forward_range').value
        self.width_margin = p('width_margin').value
        in_topic = p('input_topic').value
        out_topic = p('output_topic').value
        depth_topic = p('depth_topic').value

        # Current detection state
        self.obstacle_detected = False
        self.min_distance = math.inf

        # Publishers / subscribers
        self.cmd_pub = self.create_publisher(Twist, out_topic, 10)
        self.state_pub = self.create_publisher(String, '/depth_safety_state', 10)

        self.depth_sub = self.create_subscription(
            PointCloud2, depth_topic, self._depth_cb,
            QoSPresetProfiles.SENSOR_DATA.value)

        self.cmd_sub = self.create_subscription(
            Twist, in_topic, self._cmd_cb, 10)

        self.get_logger().info(
            f'DepthSafetyShield ready | stop={self.stop_dist}m '
            f'width={self.width_margin*2}m fwd={self.fwd_range}m')

    def _depth_cb(self, msg: PointCloud2):
        """Scan depth and detect obstacles in front."""
        self.min_distance = math.inf
        obstacle_count = 0

        points = pc2.read_points(msg, field_names=("x", "y", "z"), skip_nans=True)
        for p in points:
            x, y, z = p

            # Height filter
            if z < self.h_min or z > self.h_max:
                continue

            # Forward range filter
            if x < 0.05 or x > self.fwd_range:
                continue

            # Width filter: center corridor only
            if abs(y) > self.width_margin:
                continue

            # Found point in detection zone
            obstacle_count += 1
            self.min_distance = min(self.min_distance, x)

        # Update state
        prev_state = self.obstacle_detected
        self.obstacle_detected = (self.min_distance < self.stop_dist)

        if self.obstacle_detected != prev_state:
            status = "OBSTACLE DETECTED" if self.obstacle_detected else "CLEAR"
            self.get_logger().info(f'[SHIELD] {status} at {self.min_distance:.2f}m ({obstacle_count} pts)')

            state_msg = String()
            state_msg.data = status
            self.state_pub.publish(state_msg)

    def _cmd_cb(self, msg: Twist):
        """Filter velocity: stop forward if obstacle, allow rotation."""
        out = Twist()
        out.angular.z = msg.angular.z  # Always allow rotation

        if self.obstacle_detected:
            # Obstacle ahead: stop forward motion, allow reverse
            out.linear.x = min(msg.linear.x, 0.0)
        else:
            # Clear: passthrough
            out.linear.x = msg.linear.x

        self.cmd_pub.publish(out)


def main(args=None):
    rclpy.init(args=args)
    node = DepthSafetyShield()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.cmd_pub.publish(Twist())
        node.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    main()
