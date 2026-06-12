#!/usr/bin/env python3
"""
Depth Camera Safety Shield ? Argo Mini

Pipeline:
    Nav2 controller ? /cmd_vel_raw
    velocity_smoother ? /cmd_vel_raw ? /cmd_vel_smoothed
    [this node] ? /cmd_vel_smoothed ? /cmd_vel
    serial_bridge ? /cmd_vel ? ESP32

Behavior:
    OBSTACLE DETECTED ? Stop forward motion (allow rotation + reverse)
    NO OBSTACLE       ? Passthrough all commands

AXIS ASSUMPTION: PointCloud2 is in a frame where
    x = forward (away from robot)
    y = lateral (left/right)
    z = height  (up)
If your HP60C publishes in ROS optical frame (z=forward, x=right, y=down),
set use_optical_frame:=true to swap axes correctly.
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

        self.declare_parameter('stop_distance',    0.50)   # m: stop if obstacle closer than this
        self.declare_parameter('height_min',       0.15)   # m: ignore floor
        self.declare_parameter('height_max',       1.80)   # m: ignore ceiling
        self.declare_parameter('forward_range',    1.50)   # m: max forward detection range
        self.declare_parameter('tunnel_width',     0.30)   # m: half-width of safety corridor
        self.declare_parameter('min_points',       5)      # minimum points to confirm obstacle
        self.declare_parameter('stale_timeout',    1.0)    # s: clear state if no camera data
        self.declare_parameter('use_optical_frame', False) # swap axes for optical-convention cams
        self.declare_parameter('input_topic',      '/cmd_vel_smoothed')
        self.declare_parameter('output_topic',     '/cmd_vel')
        self.declare_parameter('depth_topic',      '/ascamera_hp60c/camera_publisher/depth0/points')

        # unused legacy params (accepted silently so launch script doesn't error)
        self.declare_parameter('move_speed',       0.0)
        self.declare_parameter('turn_speed',       0.0)
        self.declare_parameter('resume_threshold', 0.0)
        self.declare_parameter('min_search_time',  0.0)

        p = self.get_parameter
        self.stop_dist     = p('stop_distance').value
        self.h_min         = p('height_min').value
        self.h_max         = p('height_max').value
        self.fwd_range     = p('forward_range').value
        self.tunnel_width  = p('tunnel_width').value
        self.min_points    = p('min_points').value
        self.stale_timeout = p('stale_timeout').value
        self.optical_frame = p('use_optical_frame').value
        in_topic           = p('input_topic').value
        out_topic          = p('output_topic').value
        depth_topic        = p('depth_topic').value

        self.obstacle_detected = False
        self.min_distance      = math.inf
        self.last_depth_time   = self.get_clock().now()

        self.cmd_pub   = self.create_publisher(Twist, out_topic, 10)
        self.state_pub = self.create_publisher(String, '/depth_safety_state', 10)

        self.depth_sub = self.create_subscription(
            PointCloud2, depth_topic, self._depth_cb,
            QoSPresetProfiles.SENSOR_DATA.value)

        self.cmd_sub = self.create_subscription(
            Twist, in_topic, self._cmd_cb, 10)

        # watchdog: clear obstacle state if camera goes silent
        self.create_timer(0.2, self._watchdog)

        self.get_logger().info(
            f'DepthSafetyShield ready | stop={self.stop_dist}m '
            f'corridor�{self.tunnel_width}m fwd={self.fwd_range}m '
            f'min_pts={self.min_points} optical={self.optical_frame}')

    def _depth_cb(self, msg: PointCloud2):
        self.last_depth_time = self.get_clock().now()
        min_dist = math.inf
        obstacle_count = 0

        points = pc2.read_points(msg, field_names=("x", "y", "z"), skip_nans=True)
        for pt in points:
            raw_x, raw_y, raw_z = pt

            if self.optical_frame:
                # ROS optical convention: z=forward, x=right, y=down
                fwd    =  raw_z
                lateral = raw_x
                height  = -raw_y
            else:
                # Robot-body convention: x=forward, y=lateral, z=up
                fwd    = raw_x
                lateral = raw_y
                height  = raw_z

            if height < self.h_min or height > self.h_max:
                continue

            if fwd < 0.05 or fwd > self.fwd_range:
                continue

            if abs(lateral) > self.tunnel_width:
                continue

            obstacle_count += 1
            min_dist = min(min_dist, fwd)

        # require minimum point count to avoid single noisy pixel stopping the robot
        prev = self.obstacle_detected
        if obstacle_count >= self.min_points:
            self.obstacle_detected = (min_dist < self.stop_dist)
            self.min_distance = min_dist
        else:
            self.obstacle_detected = False
            self.min_distance = math.inf

        if self.obstacle_detected != prev:
            status = "OBSTACLE DETECTED" if self.obstacle_detected else "CLEAR"
            self.get_logger().info(
                f'[SHIELD] {status} | dist={self.min_distance:.2f}m pts={obstacle_count}')
            msg_out = String()
            msg_out.data = status
            self.state_pub.publish(msg_out)

    def _watchdog(self):
        """If no depth frame received recently, clear obstacle state so robot isn't stuck."""
        dt = (self.get_clock().now() - self.last_depth_time).nanoseconds / 1e9
        if dt > self.stale_timeout and self.obstacle_detected:
            self.obstacle_detected = False
            self.min_distance = math.inf
            self.get_logger().warn(
                f'[SHIELD] No depth data for {dt:.1f}s ? clearing obstacle state')
            msg = String()
            msg.data = "CLEAR (stale)"
            self.state_pub.publish(msg)

    def _cmd_cb(self, msg: Twist):
        out = Twist()
        out.angular.z = msg.angular.z  # always allow rotation

        if self.obstacle_detected:
            out.linear.x = min(msg.linear.x, 0.0)  # block forward, allow reverse
        else:
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
