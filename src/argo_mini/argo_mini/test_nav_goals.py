#!/usr/bin/env python3
"""
Simple goal sender for testing navigation
Run: python3 test_nav_goals.py --goal 1   # Go to waypoint 1
"""

import argparse
import rclpy
from std_msgs.msg import Int32


def main():
    parser = argparse.ArgumentParser(description='Send navigation goal')
    parser.add_argument('--goal', type=int, required=True, help='Waypoint ID (0=kitchen, 1=table1, etc.)')
    args = parser.parse_args()

    rclpy.init()
    node = rclpy.create_node('test_goal_sender')
    pub = node.create_publisher(Int32, '/dashboard_waypoint_cmd', 10)

    msg = Int32()
    msg.data = args.goal

    # Publish a few times to ensure delivery
    for _ in range(3):
        pub.publish(msg)
        node.get_logger().info(f'Sent goal: {args.goal}')

    node.destroy_node()
    rclpy.shutdown()


if __name__ == '__main__':
    main()
