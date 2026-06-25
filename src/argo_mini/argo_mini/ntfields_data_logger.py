#!/usr/bin/env python3
"""
Mode 2A: NTFields data logger.

Records (pose, lidar_scan) frames while the robot explores a new environment
with the Nav2 SLAM stack. The saved NPZ files feed ntfields_offline_train.py.

Topics:
    /scan           (sensor_msgs/LaserScan)
    /tf             (base_link → map)

Services:
    ~/start_logging  (std_srvs/Trigger)  begin recording
    ~/stop_logging   (std_srvs/Trigger)  stop + save
    ~/status         (std_srvs/Trigger)  print frame count
"""

import math
import os
import threading
import time

import numpy as np
import rclpy
from rclpy.node import Node
from sensor_msgs.msg import LaserScan
from std_srvs.srv import Trigger
import tf2_ros
from tf2_ros import TransformException


class NTFieldsDataLogger(Node):

    def __init__(self):
        super().__init__('ntfields_data_logger')

        self.declare_parameter('output_dir',    '~/ntfields_data')
        self.declare_parameter('min_move_dist', 0.30)
        self.declare_parameter('min_move_rad',  0.20)
        self.declare_parameter('max_frames',    5000)

        self._out_dir    = os.path.expanduser(
            self.get_parameter('output_dir').value)
        self._min_dist   = self.get_parameter('min_move_dist').value
        self._min_rad    = self.get_parameter('min_move_rad').value
        self._max_frames = self.get_parameter('max_frames').value

        self._logging   = False
        self._frames    = []
        self._last_pose = None
        self._lock      = threading.Lock()

        self._tf_buf    = tf2_ros.Buffer()
        self._tf_listen = tf2_ros.TransformListener(self._tf_buf, self)

        self.create_subscription(LaserScan, '/scan', self._scan_cb, 10)
        self.create_service(Trigger, '~/start_logging', self._start_cb)
        self.create_service(Trigger, '~/stop_logging',  self._stop_cb)
        self.create_service(Trigger, '~/status',        self._status_cb)
        self.create_timer(15.0, self._log_status)

        self.get_logger().info(
            f'[DataLogger] ready  output={self._out_dir}  '
            f'min_move={self._min_dist:.2f}m/{self._min_rad:.2f}rad')

    # ── Scan callback ──────────────────────────────────────────────────────────

    def _scan_cb(self, msg: LaserScan):
        if not self._logging:
            return

        pose = self._get_pose()
        if pose is None:
            return
        x, y, theta = pose

        with self._lock:
            if self._last_pose is not None:
                lx, ly, lt = self._last_pose
                dist = math.sqrt((x - lx) ** 2 + (y - ly) ** 2)
                dang = abs(theta - lt) % (2 * 3.14159)
                dang = min(dang, 2 * 3.14159 - dang)
                if dist < self._min_dist and dang < self._min_rad:
                    return

            if len(self._frames) >= self._max_frames:
                self.get_logger().warn(
                    f'[DataLogger] max frames ({self._max_frames}) reached – saving')
                self._logging = False
                frames = list(self._frames)
            else:
                self._last_pose = (x, y, theta)
                self._frames.append({
                    'x':     float(x),
                    'y':     float(y),
                    'theta': float(theta),
                    'stamp': (msg.header.stamp.sec
                              + msg.header.stamp.nanosec * 1e-9),
                    'angle_min': float(msg.angle_min),
                    'angle_inc': float(msg.angle_increment),
                    'range_max': float(msg.range_max),
                    'ranges':    np.array(msg.ranges, dtype=np.float32),
                })
                return

        # Reached max_frames – save outside lock
        self._do_save(frames)

    def _get_pose(self):
        try:
            t    = self._tf_buf.lookup_transform('map', 'base_link',
                                                  rclpy.time.Time())
            tx   = t.transform.translation.x
            ty   = t.transform.translation.y
            q    = t.transform.rotation
            siny = 2.0 * (q.w * q.z + q.x * q.y)
            cosy = 1.0 - 2.0 * (q.y * q.y + q.z * q.z)
            yaw  = float(np.arctan2(siny, cosy))
            return tx, ty, yaw
        except TransformException:
            return None

    # ── Services ──────────────────────────────────────────────────────────────

    def _start_cb(self, req, res):
        os.makedirs(self._out_dir, exist_ok=True)
        with self._lock:
            self._logging   = True
            self._frames    = []
            self._last_pose = None
        self.get_logger().info('[DataLogger] logging STARTED')
        res.success = True;  res.message = 'Logging started'
        return res

    def _stop_cb(self, req, res):
        with self._lock:
            self._logging = False
            frames = list(self._frames)
        self.get_logger().info(f'[DataLogger] logging STOPPED – {len(frames)} frames')
        path = ''
        if frames:
            path = self._do_save(frames)
        res.success = True
        res.message = f'Saved {len(frames)} frames to {path}' if frames else 'No frames recorded'
        return res

    def _status_cb(self, req, res):
        with self._lock:
            n, active = len(self._frames), self._logging
        res.success = True
        res.message = f'{"LOGGING" if active else "IDLE"}  frames={n}'
        return res

    def _log_status(self):
        with self._lock:
            n, active = len(self._frames), self._logging
        if active:
            self.get_logger().info(f'[DataLogger] {n} frames logged')

    # ── Save ──────────────────────────────────────────────────────────────────

    def _do_save(self, frames: list) -> str:
        ts   = time.strftime('%Y%m%d_%H%M%S')
        path = os.path.join(self._out_dir, f'scan_log_{ts}.npz')
        np.savez_compressed(path,
            x         = np.array([f['x']         for f in frames], dtype=np.float32),
            y         = np.array([f['y']         for f in frames], dtype=np.float32),
            theta     = np.array([f['theta']     for f in frames], dtype=np.float32),
            stamp     = np.array([f['stamp']     for f in frames], dtype=np.float64),
            angle_min = np.array([f['angle_min'] for f in frames], dtype=np.float32),
            angle_inc = np.array([f['angle_inc'] for f in frames], dtype=np.float32),
            range_max = np.array([f['range_max'] for f in frames], dtype=np.float32),
            ranges    = np.array([f['ranges']    for f in frames], dtype=np.float32),
        )
        self.get_logger().info(f'[DataLogger] {len(frames)} frames saved → {path}')
        return path


def main(args=None):
    rclpy.init(args=args)
    node = NTFieldsDataLogger()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    main()
