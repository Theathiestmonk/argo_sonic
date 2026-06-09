#!/usr/bin/env python3
"""
Depth Camera Safety Shield ? Argo Mini

Pipeline:
    Nav2 controller ? /cmd_vel_raw
    velocity_smoother ? /cmd_vel_raw  ?  /cmd_vel_smoothed
    [this node]       ? /cmd_vel_smoothed  ?  /cmd_vel
    serial_bridge     ? /cmd_vel  ?  ESP32

Behaviour:
    CLEAR  (no obstacle within slow_distance) ? pass cmd_vel through unchanged
    SLOW   (obstacle between stop_distance and slow_distance) ? scale linear.x
    STOP   (obstacle within stop_distance) ? zero forward motion (reverse allowed)
    STALE  (no depth frame received in depth_timeout seconds) ? pass through (fail-safe)

The node also re-publishes the depth PointCloud2 (downsampled) on /depth_filtered
so Nav2's local costmap can use it as an observation source for proactive planning.
"""

import math
import time

import numpy as np
import rclpy
from geometry_msgs.msg import Twist
from rclpy.duration import Duration
from rclpy.node import Node
from rclpy.qos import QoSPresetProfiles
from rclpy.time import Time
from sensor_msgs.msg import PointCloud2
from sensor_msgs_py import point_cloud2 as pc2
from std_msgs.msg import Header
import tf2_ros


class DepthSafetyShield(Node):

    # ?? state constants ??????????????????????????????????????????????????????
    CLEAR = "CLEAR"
    SLOW  = "SLOW"
    STOP  = "STOP"
    STALE = "STALE"
    AVOIDANCE = "AVOIDANCE"

    def __init__(self):
        super().__init__('depth_safety_shield')

        # ?? parameters ???????????????????????????????????????????????????????
        self.declare_parameter('stop_distance',       0.40)   # m ? hard stop threshold (was 0.70)
        self.declare_parameter('slow_distance',       0.65)   # m ? begin scaling (was 1.0)
        self.declare_parameter('slow_factor',         0.30)   # fraction of linear.x (was 0.40)
        self.declare_parameter('lateral_margin',      0.40)   # m ? half robot width + buffer
        self.declare_parameter('min_obstacle_height', 0.05)   # m ? ignore floor reflections
        self.declare_parameter('max_obstacle_height', 1.60)   # m ? ignore overhead structure
        self.declare_parameter('depth_timeout',       3.0)    # s ? stale-data window
        self.declare_parameter('downsample_stride',   4)      # process every Nth point row/col
        self.declare_parameter('input_topic',  '/cmd_vel_smoothed')
        self.declare_parameter('output_topic', '/cmd_vel')
        self.declare_parameter('depth_topic',
            '/ascamera_hp60c/camera_publisher/depth0/points')
        self.declare_parameter('enable_lateral_avoidance', True)  # NEW: Enable zone-based avoidance
        self.declare_parameter('avoidance_angular_speed',   0.5)  # rad/s: rotation speed for avoidance
        self.declare_parameter('left_zone_angle',    0.3)   # rad: left zone boundary
        self.declare_parameter('right_zone_angle',   0.3)   # rad: right zone boundary

        p = self.get_parameter
        self.stop_dist   = p('stop_distance').value
        self.slow_dist   = p('slow_distance').value
        self.slow_factor = p('slow_factor').value
        self.lat_margin  = p('lateral_margin').value
        self.min_h       = p('min_obstacle_height').value
        self.max_h       = p('max_obstacle_height').value
        self.timeout     = p('depth_timeout').value
        self.stride      = p('downsample_stride').value
        in_topic         = p('input_topic').value
        out_topic        = p('output_topic').value
        depth_topic      = p('depth_topic').value
        self.enable_avoidance = p('enable_lateral_avoidance').value
        self.avoidance_speed = p('avoidance_angular_speed').value
        self.left_zone   = p('left_zone_angle').value
        self.right_zone  = p('right_zone_angle').value

        # ?? TF ???????????????????????????????????????????????????????????????
        self.tf_buffer   = tf2_ros.Buffer()
        self.tf_listener = tf2_ros.TransformListener(self.tf_buffer, self)

        # ?? state ????????????????????????????????????????????????????????????
        self.closest_fwd   = math.inf   # m ? nearest obstacle in forward zone
        self.closest_left  = math.inf   # m ? nearest obstacle in left zone
        self.closest_right = math.inf   # m ? nearest obstacle in right zone
        self.last_depth_ts = None       # monotonic time of last depth frame
        self.state         = self.STALE
        self.avoidance_dir = 0          # -1 (left), 0 (none), +1 (right)

        # Rate-limit depth processing: track last processed time
        self._last_proc    = 0.0
        self._proc_interval = 0.20      # ~5 Hz processing cap (reduced for Jetson)

        # ?? publishers / subscribers ?????????????????????????????????????????
        self.cmd_pub = self.create_publisher(Twist, out_topic, 10)

        # Re-publish filtered cloud so Nav2 costmap can use it
        self.cloud_pub = self.create_publisher(
            PointCloud2, '/depth_filtered', QoSPresetProfiles.SENSOR_DATA.value)

        # Publish safety state (STOP, SLOW, CLEAR, STALE) so other nodes can react
        from std_msgs.msg import String
        self.state_pub = self.create_publisher(String, '/depth_safety_state', 10)

        # Use SENSOR_DATA QoS (best-effort, depth-1) to keep only latest frame
        self.depth_sub = self.create_subscription(
            PointCloud2, depth_topic, self._depth_cb,
            QoSPresetProfiles.SENSOR_DATA.value)

        self.cmd_sub = self.create_subscription(
            Twist, in_topic, self._cmd_cb, 10)

        # Watchdog: mark STALE if no depth arrives
        self.create_timer(1.0, self._watchdog)

        self.get_logger().info(
            f'DepthSafetyShield ready  |  '
            f'stop={self.stop_dist}m  slow={self.slow_dist}m  '
            f'in={in_topic}  out={out_topic}')

    # ?? depth callback ????????????????????????????????????????????????????????
    def _depth_cb(self, msg: PointCloud2):
        now = time.monotonic()

        # Rate-limit heavy processing
        if now - self._last_proc < self._proc_interval:
            self.last_depth_ts = now   # still mark as alive
            return
        self._last_proc = now

        # Look up TF from camera frame ? base_link
        # Use latest available transform (time=0) to avoid timestamp skew issues
        try:
            tf_stamped = self.tf_buffer.lookup_transform(
                'base_link',
                msg.header.frame_id,
                rclpy.time.Time(seconds=0),
                timeout=Duration(seconds=0.2))
        except (tf2_ros.LookupException,
                tf2_ros.ConnectivityException,
                tf2_ros.ExtrapolationException) as e:
            self.get_logger().warn(f'TF lookup failed: {e}')
            self.last_depth_ts = now
            return

        # Read XYZ points (downsampled by stride).
        # Use read_points with field_names and convert via list comprehension to
        # handle non-packed layouts (e.g. 16-byte stride with 4-byte padding).
        try:
            pts_raw = np.array(
                [(p[0], p[1], p[2])
                 for p in pc2.read_points(
                     msg, field_names=('x', 'y', 'z'), skip_nans=True)],
                dtype=np.float32)
        except Exception as e:
            self.get_logger().warn(f'PointCloud2 read error: {e}')
            self.last_depth_ts = now
            return

        self.last_depth_ts = now

        if pts_raw.size == 0:
            self.closest_fwd = math.inf
            self._update_state()
            return

        # Stride-downsample for efficiency
        pts = pts_raw[::self.stride]  # Nx3

        # ?? Transform to base_link ????????????????????????????????????????????
        t  = tf_stamped.transform.translation
        r  = tf_stamped.transform.rotation
        qx, qy, qz, qw = r.x, r.y, r.z, r.w

        # Rotation matrix from quaternion (vectorised)
        R = np.array([
            [1 - 2*(qy*qy + qz*qz),   2*(qx*qy - qz*qw),   2*(qx*qz + qy*qw)],
            [    2*(qx*qy + qz*qw), 1 - 2*(qx*qx + qz*qz),  2*(qy*qz - qx*qw)],
            [    2*(qx*qz - qy*qw),    2*(qy*qz + qx*qw),  1 - 2*(qx*qx + qy*qy)]
        ], dtype=np.float64)

        pts_bl = (R @ pts.T).T + np.array([t.x, t.y, t.z])  # Nx3 in base_link

        x = pts_bl[:, 0]
        y = pts_bl[:, 1]
        z = pts_bl[:, 2]

        # ?? Zone-based filtering ??????????????????????????????????????????????
        # Divide forward space into left, center, right zones
        height_ok = (z > self.min_h) & (z < self.max_h)
        in_range = (x > 0.05) & (x < self.slow_dist)

        # Center zone: -left_zone < y < right_zone (straight ahead)
        center_mask = height_ok & in_range & (np.abs(y) < self.lat_margin)
        center_x = x[center_mask]
        self.closest_fwd = float(np.min(center_x)) if center_x.size > 0 else math.inf

        # Left zone: y >= left_zone threshold
        left_mask = height_ok & in_range & (y >= self.lat_margin)
        left_x = x[left_mask]
        self.closest_left = float(np.min(left_x)) if left_x.size > 0 else math.inf

        # Right zone: y <= -right_zone threshold
        right_mask = height_ok & in_range & (y <= -self.lat_margin)
        right_x = x[right_mask]
        self.closest_right = float(np.min(right_x)) if right_x.size > 0 else math.inf

        self._update_state()

        # Re-publish filtered points (in base_link frame) for costmap
        # Include all zones (left + center + right) for costmap
        all_zones_mask = center_mask | left_mask | right_mask
        if np.any(all_zones_mask):
            keep = pts_bl[all_zones_mask]
            header = Header()
            header.stamp    = msg.header.stamp
            header.frame_id = 'base_link'
            filtered_msg = pc2.create_cloud_xyz32(header, keep.tolist())
            self.cloud_pub.publish(filtered_msg)

    # ?? cmd_vel callback ??????????????????????????????????????????????????????
    def _cmd_cb(self, msg: Twist):
        out = Twist()
        out.angular.z = msg.angular.z  # angular always passes through

        if self.state == self.STALE:
            # No depth data ? fail-safe: pass through unchanged
            out.linear.x = msg.linear.x

        elif self.state == self.STOP:
            # Hard stop forward motion; allow reverse so Nav2 can recover
            out.linear.x = min(msg.linear.x, 0.0)

            # Implement lateral avoidance by commanding rotation
            if self.enable_avoidance and self.avoidance_dir != 0:
                out.angular.z = self.avoidance_speed * self.avoidance_dir

        elif self.state == self.SLOW:
            # Scale linear to slow_factor, always allow reverse
            if msg.linear.x > 0.0:
                out.linear.x = msg.linear.x * self.slow_factor
            else:
                out.linear.x = msg.linear.x

        else:  # CLEAR
            out.linear.x = msg.linear.x

        self.cmd_pub.publish(out)

    # ?? helpers ???????????????????????????????????????????????????????????????
    def _update_state(self):
        d_fwd   = self.closest_fwd
        d_left  = self.closest_left
        d_right = self.closest_right
        prev_state = self.state
        prev_dir = self.avoidance_dir

        # Hard stop: obstacle within stop_distance in center
        if d_fwd <= self.stop_dist:
            self.state = self.STOP
            # Determine which side to avoid
            if self.enable_avoidance:
                if d_left > d_right:
                    self.avoidance_dir = -1  # Turn left
                elif d_right > d_left:
                    self.avoidance_dir = 1   # Turn right
                else:
                    self.avoidance_dir = 0   # Both blocked equally
            else:
                self.avoidance_dir = 0

        # Slow: obstacle in slow_distance but not stop_distance
        elif d_fwd <= self.slow_dist:
            self.state = self.SLOW
            self.avoidance_dir = 0

        # Clear: no obstacle ahead
        else:
            self.state = self.CLEAR
            self.avoidance_dir = 0

        if self.state != prev_state or self.avoidance_dir != prev_dir:
            self.get_logger().info(
                f'Safety state: {prev_state} ? {self.state}  '
                f'(fwd={d_fwd:.2f}m, left={d_left:.2f}m, right={d_right:.2f}m)  '
                f'avoidance_dir={self.avoidance_dir}')
            # Publish state for other nodes (allow reverse only in STOP state)
            state_msg = String()
            state_msg.data = self.state
            self.state_pub.publish(state_msg)

    def _watchdog(self):
        if self.last_depth_ts is None:
            if self.state != self.STALE:
                self.get_logger().warn('No depth data yet ? safety in STALE (pass-through)')
                self.state = self.STALE
            return

        age = time.monotonic() - self.last_depth_ts
        if age > self.timeout:
            if self.state != self.STALE:
                self.get_logger().warn(
                    f'Depth data stale ({age:.1f}s) ? reverting to STALE (pass-through)')
                # Note: if you need throttling, implement a throttle timestamp instead
                self.state = self.STALE


def main(args=None):
    rclpy.init(args=args)
    node = DepthSafetyShield()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    main()
