#!/usr/bin/env python3
"""
Automatic pose initializer for Argo Mini
Reads kitchen location from office_map.json and sets robot pose at startup
"""

import json
import math
import time
from pathlib import Path

import rclpy
from rclpy.node import Node
from geometry_msgs.msg import PoseWithCovarianceStamped


class PoseInit(Node):
    """Initialize robot pose at kitchen on startup."""

    def __init__(self):
        super().__init__('pose_init')

        # Load office_map.json
        map_file = Path(__file__).parent.parent / "waypoints" / "office_map.json"
        with open(map_file) as f:
            waypoints = json.load(f)

        # Find kitchen waypoint
        kitchen = None
        for wp_id, wp in waypoints.items():
            if wp.get("name") == "Kitchen":
                kitchen = wp
                break

        if not kitchen:
            self.get_logger().error("ERROR: Kitchen not found in office_map.json")
            raise RuntimeError("Kitchen waypoint not found")

        # Extract pose
        x = kitchen['x']
        y = kitchen['y']
        theta = kitchen.get('theta', 0.0)

        self.get_logger().info(f'Kitchen: x={x:.3f}, y={y:.3f}, theta={math.degrees(theta):.1f}°')

        # Publish initial pose
        pub = self.create_publisher(PoseWithCovarianceStamped, '/initialpose', 10)
        time.sleep(0.5)  # Wait for subscriber

        msg = PoseWithCovarianceStamped()
        msg.header.frame_id = 'map'
        msg.header.stamp = self.get_clock().now().to_msg()

        msg.pose.pose.position.x = x
        msg.pose.pose.position.y = y
        msg.pose.pose.position.z = 0.0

        msg.pose.pose.orientation.x = 0.0
        msg.pose.pose.orientation.y = 0.0
        msg.pose.pose.orientation.z = math.sin(theta / 2.0)
        msg.pose.pose.orientation.w = math.cos(theta / 2.0)

        # Covariance
        msg.pose.covariance[0] = 0.25   # x uncertainty
        msg.pose.covariance[7] = 0.25   # y uncertainty
        msg.pose.covariance[35] = 0.1   # theta uncertainty

        pub.publish(msg)
        self.get_logger().info('✓ Initial pose published to /initialpose')

        # Exit after publishing
        time.sleep(1.0)
        rclpy.shutdown()


def main(args=None):
    rclpy.init(args=args)
    node = PoseInit()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    main()
