#!/usr/bin/env python3
"""
ARGO Mini motor test teleop for the supplied ESP32 firmware.

IMPORTANT:
- This sends signed DAC commands, NOT RPM.
- Test with the robot raised securely so drive wheels cannot touch the floor.
- Keep an emergency stop/power disconnect within reach.
- SPACE sends S (stop); Ctrl+C exits.
- The ESP32 firmware clamps every non-zero command to DAC_MIN..DAC_MAX.
"""

import select
import sys
import termios
import time
import tty

import rclpy
from rclpy.node import Node
from geometry_msgs.msg import Twist


class ArgoDacTestTeleop(Node):
    def __init__(self):
        super().__init__("argo_dac_test_teleop")
        self.pub = self.create_publisher(Twist, "/cmd_vel", 10)
        self.lin = 0.0
        self.ang = 0.0
        self.last_key_time = time.monotonic()
        self.key_timeout = 0.25
        self.create_timer(0.05, self.tick)
        self.get_logger().info(
            "Test teleop ready. W/S forward/reverse, A/D turn, SPACE stop, Ctrl+C quit."
        )
        self.get_logger().info(
            "This node publishes low-speed Twist commands. It assumes the test bridge below is running."
        )

    def tick(self):
        if time.monotonic() - self.last_key_time > self.key_timeout:
            self.lin = 0.0
            self.ang = 0.0
        msg = Twist()
        msg.linear.x = self.lin
        msg.angular.z = self.ang
        self.pub.publish(msg)

    def set_key(self, ch):
        self.last_key_time = time.monotonic()
        if ch in ("w", "W", "\x1b[A"):
            self.lin, self.ang = 0.10, 0.0
        elif ch in ("s", "S", "\x1b[B"):
            self.lin, self.ang = -0.10, 0.0
        elif ch in ("a", "A", "\x1b[D"):
            self.lin, self.ang = 0.0, 0.30
        elif ch in ("d", "D", "\x1b[C"):
            self.lin, self.ang = 0.0, -0.30
        elif ch == " ":
            self.lin, self.ang = 0.0, 0.0

    def stop(self):
        self.lin = 0.0
        self.ang = 0.0
        msg = Twist()
        for _ in range(3):
            self.pub.publish(msg)
            time.sleep(0.05)


def read_char(fd):
    ch = sys.stdin.read(1)
    if ch == "\x1b":
        # Arrow keys arrive as ESC [ A/B/C/D.
        ch += sys.stdin.read(2)
    return ch


def main():
    rclpy.init()
    node = ArgoDacTestTeleop()
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        while rclpy.ok():
            # Spin ROS callbacks/timer while waiting briefly for keyboard input.
            rclpy.spin_once(node, timeout_sec=0.02)
            ready, _, _ = select.select([fd], [], [], 0)
            if ready:
                ch = read_char(fd)
                if ch in ("\x03", "\x04"):
                    break
                node.set_key(ch)
    except KeyboardInterrupt:
        pass
    finally:
        try:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
        except Exception:
            pass
        node.stop()
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == "__main__":
    main()
