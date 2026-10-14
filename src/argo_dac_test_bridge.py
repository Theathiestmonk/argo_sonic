#!/usr/bin/env python3
"""
ARGO Mini TEST serial bridge for the exact supplied ESP32 firmware.

ESP32 protocol:
  Receive: V <left_dac> <right_dac>   signed DAC values
           S                           stop
  Send:    O <leftTicks> <rightTicks>

This bridge intentionally converts /cmd_vel into small signed DAC commands.
It does NOT send RPM. Use only for a controlled motor test, not SLAM/Nav2.
Stop all other serial_bridge/teleop processes before running this file.
"""

import math
import time

import rclpy
import serial
from rclpy.node import Node
from geometry_msgs.msg import Twist
from nav_msgs.msg import Odometry


WHEEL_BASE = 0.41
WHEEL_RADIUS = 0.08255
# Start at the lowest command accepted by the firmware. This is a TEST mapping,
# not a calibrated speed controller. Tune only after checking the ESC input spec.
TEST_DAC = 104
MAX_DAC = 120


class ArgoDacTestBridge(Node):
    def __init__(self):
        super().__init__("argo_dac_test_bridge")
        self.declare_parameter("port", "/dev/esp32")
        self.declare_parameter("baud", 115200)
        self.declare_parameter("test_dac", TEST_DAC)
        self.declare_parameter("max_dac", MAX_DAC)
        self.declare_parameter("forward_only", False)

        port = self.get_parameter("port").value
        baud = int(self.get_parameter("baud").value)
        self.test_dac = int(self.get_parameter("test_dac").value)
        self.max_dac = int(self.get_parameter("max_dac").value)
        self.forward_only = bool(self.get_parameter("forward_only").value)

        if not (104 <= self.test_dac <= self.max_dac <= 120):
            raise ValueError("Expected 104 <= test_dac <= max_dac <= 120 for this firmware.")

        self.ser = serial.Serial(port, baud, timeout=0.02, write_timeout=0.1)
        time.sleep(2.0)
        self.ser.reset_input_buffer()

        self.pub_odom = self.create_publisher(Odometry, "/odom", 10)
        self.sub = self.create_subscription(Twist, "/cmd_vel", self.cmd_cb, 10)
        self.timer = self.create_timer(0.02, self.read_serial)
        self.last_cmd_time = time.monotonic()
        self.last_left_ticks = None
        self.last_right_ticks = None
        self.x = self.y = self.theta = 0.0
        self.prev_time = time.monotonic()
        self.get_logger().info(
            f"TEST bridge connected to {port} at {baud}; test_dac={self.test_dac}. "
            "This is not for SLAM/Nav2."
        )

    def cmd_cb(self, msg: Twist):
        self.last_cmd_time = time.monotonic()
        lin = float(msg.linear.x)
        ang = float(msg.angular.z)

        if self.forward_only and lin < 0:
            lin = 0.0

        # For this test, use direction and presence of wheel velocity only.
        left = lin - ang * WHEEL_BASE / 2.0
        right = lin + ang * WHEEL_BASE / 2.0

        def to_dac(v):
            if abs(v) < 0.01:
                return 0
            return int(math.copysign(self.test_dac, v))

        l_cmd, r_cmd = to_dac(left), to_dac(right)
        # Do not permit an accidental zero-speed spin test with opposite wheel
        # directions unless the operator intentionally commands a turn.
        try:
            line = f"V {l_cmd} {r_cmd}\n"
            self.ser.write(line.encode("ascii"))
            self.get_logger().info(
                f"/cmd_vel lin={lin:+.3f} ang={ang:+.3f} -> {line.strip()}"
            )
        except serial.SerialException as exc:
            self.get_logger().error(f"Serial write failed: {exc}")

    def read_serial(self):
        try:
            while self.ser.in_waiting:
                line = self.ser.readline().decode("utf-8", errors="ignore").strip()
                if not line:
                    break
                if line.startswith("O "):
                    p = line.split()
                    if len(p) == 3:
                        self.update_odom(int(p[1]), int(p[2]))
                elif line in ("READY", "STOP"):
                    self.get_logger().info(f"ESP32: {line}")
        except (ValueError, serial.SerialException) as exc:
            self.get_logger().warning(f"Serial read error: {exc}")

    def update_odom(self, left_ticks, right_ticks):
        if self.last_left_ticks is None:
            self.last_left_ticks, self.last_right_ticks = left_ticks, right_ticks
            self.prev_time = time.monotonic()
            return

        now = time.monotonic()
        dt = now - self.prev_time
        self.prev_time = now
        dl = (left_ticks - self.last_left_ticks) * (2 * math.pi * WHEEL_RADIUS) / 60.0
        dr = (right_ticks - self.last_right_ticks) * (2 * math.pi * WHEEL_RADIUS) / 60.0
        self.last_left_ticks, self.last_right_ticks = left_ticks, right_ticks

        # Reject huge deltas that can result from a serial glitch.
        if abs(dl) > 0.10 or abs(dr) > 0.10:
            self.get_logger().warning("Ignoring implausible encoder tick jump.")
            return

        dtheta = (dr - dl) / WHEEL_BASE
        dcenter = (dl + dr) / 2.0
        self.x += dcenter * math.cos(self.theta + dtheta / 2.0)
        self.y += dcenter * math.sin(self.theta + dtheta / 2.0)
        self.theta += dtheta

        odom = Odometry()
        odom.header.stamp = self.get_clock().now().to_msg()
        odom.header.frame_id = "odom"
        odom.child_frame_id = "base_footprint"
        odom.pose.pose.position.x = self.x
        odom.pose.pose.position.y = self.y
        odom.pose.pose.orientation.z = math.sin(self.theta / 2.0)
        odom.pose.pose.orientation.w = math.cos(self.theta / 2.0)
        if dt > 0:
            odom.twist.twist.linear.x = dcenter / dt
            odom.twist.twist.angular.z = dtheta / dt
        self.pub_odom.publish(odom)

    def destroy_node(self):
        try:
            self.ser.write(b"S\n")
            self.ser.flush()
            self.ser.close()
        except Exception:
            pass
        super().destroy_node()


def main():
    rclpy.init()
    node = None
    try:
        node = ArgoDacTestBridge()
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        if node is not None:
            node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == "__main__":
    main()
