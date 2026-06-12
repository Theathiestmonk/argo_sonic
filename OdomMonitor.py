#!/usr/bin/env python3

import rclpy
import math
from rclpy.node import Node
from nav_msgs.msg import Odometry

class OdomMonitor(Node):
    def __init__(self):
        super().__init__('odom_monitor')
        self.create_subscription(
            Odometry,
            '/odom',
            self.callback,
            10
        )

    def callback(self, msg):
        x = msg.pose.pose.position.x
        y = msg.pose.pose.position.y

        qz = msg.pose.pose.orientation.z
        qw = msg.pose.pose.orientation.w

        theta = 2 * math.atan2(qz, qw)

        print(
            f"\rx={x:.3f}  y={y:.3f}  theta={theta:.3f} rad ({math.degrees(theta):.1f}°)",
            end=""
        )

def main():
    rclpy.init()
    node = OdomMonitor()
    rclpy.spin(node)

if __name__ == '__main__':
    main()
