#!/usr/bin/env python3
"""
Argo Mini — Angular-velocity bias checker

Subscribes to /cmd_vel while the robot is nav-driving and reports a clean
mean/stdev summary of angular.z, instead of a wall of raw echo numbers that
are hard to read a real bias out of by eye.

Only samples taken while linear.x is above MIN_LINEAR (i.e. actually driving
forward, not stopped/turning-in-place/at a goal) are counted, so a real turn
or goal-orientation adjustment doesn't contaminate the "should be going
straight" statistics.

Usage:
    ros2 run argo_mini check_angular_bias   (if registered as an entry point)
    python3 check_angular_bias.py [duration_seconds] [min_linear_x]
    python3 check_angular_bias.py 15 0.03
"""

import sys
import statistics
import rclpy
from rclpy.node import Node
from geometry_msgs.msg import Twist

DURATION   = float(sys.argv[1]) if len(sys.argv) > 1 else 15.0
MIN_LINEAR = float(sys.argv[2]) if len(sys.argv) > 2 else 0.03


class BiasChecker(Node):
    def __init__(self):
        super().__init__('bias_checker')
        self.samples = []
        self.skipped = 0
        self.create_subscription(Twist, '/cmd_vel', self._on_cmd, 20)

    def _on_cmd(self, msg: Twist):
        if abs(msg.linear.x) >= MIN_LINEAR:
            self.samples.append(msg.angular.z)
        else:
            self.skipped += 1


def main():
    rclpy.init()
    node = BiasChecker()
    print(f'Listening on /cmd_vel for {DURATION:.0f}s '
          f'(only counting samples with |linear.x| >= {MIN_LINEAR}) ...')
    import time
    end = time.time() + DURATION
    while time.time() < end:
        rclpy.spin_once(node, timeout_sec=0.05)

    n = len(node.samples)
    print(f'\n{"─" * 55}')
    print(f'  Samples counted (driving straight-ish): {n}')
    print(f'  Samples skipped (stopped/turning in place): {node.skipped}')

    if n < 5:
        print('  Not enough samples — was the robot actually driving forward?')
    else:
        mean = statistics.mean(node.samples)
        stdev = statistics.pstdev(node.samples)
        lo, hi = min(node.samples), max(node.samples)
        print(f'  angular.z  mean : {mean:+.4f} rad/s')
        print(f'  angular.z  stdev: {stdev:.4f} rad/s')
        print(f'  angular.z  range: [{lo:+.4f}, {hi:+.4f}]')
        print()
        if abs(mean) < stdev * 0.5:
            print('  → mean is small relative to the noise (stdev) — looks like')
            print('    zero-mean control noise, not a directional bias.')
        else:
            side = 'left (CCW, +z)' if mean > 0 else 'right (CW, -z)'
            print(f'  → mean is significant relative to the noise — the controller')
            print(f'    is persistently steering {side}. That would explain real drift.')
    print(f'{"─" * 55}')

    node.destroy_node()
    rclpy.shutdown()


if __name__ == '__main__':
    main()
