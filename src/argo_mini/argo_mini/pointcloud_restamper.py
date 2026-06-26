#!/usr/bin/env python3
"""
pointcloud_restamper.py

Fixes two issues with the raw HP60C pointcloud before it reaches the costmap:
  1. Hardware timestamp lag  → override with ROS system time
  2. Wrong frame_id          → remap to depth_camera_optical_frame (URDF TF)
  3. Depth noise             → statistical outlier removal via numpy

Statistical outlier removal (no PCL dependency):
  For each point, count neighbours within RADIUS. Points with fewer than
  MIN_NEIGHBOURS neighbours are isolated noise → removed.
  This is an approximate voxel-grid approach using a 3D bin count, which
  runs fast enough on a Jetson at 30 fps.
"""

import numpy as np
import rclpy
from rclpy.node import Node
from rclpy.qos import qos_profile_sensor_data
from sensor_msgs.msg import PointCloud2

# ── Noise filter ──────────────────────────────────────────────────────────────
FILTER_VOXEL_SIZE  = 0.08   # m  – voxel bin size for neighbour counting
MIN_NEIGHBOURS     = 3       # points in same voxel to be considered real
MAX_RANGE          = 2.0     # m  – discard beyond this (matches obstacle_range)


class PointCloudRestamper(Node):
    def __init__(self):
        super().__init__('pointcloud_restamper')

        self.subscription = self.create_subscription(
            PointCloud2,
            '/ascamera_hp60c/camera_publisher/depth0/points',
            self.listener_callback,
            qos_profile_sensor_data)

        self.publisher = self.create_publisher(
            PointCloud2,
            '/ascamera_hp60c/camera_publisher/depth0/points_corrected',
            qos_profile_sensor_data)

        self.get_logger().info(
            'PointCloud Re-Stamper Active – '
            'timestamp fix + frame remap + outlier removal')

    def listener_callback(self, msg: PointCloud2):
        msg.header.stamp = self.get_clock().now().to_msg()
        msg.header.frame_id = 'depth_camera_optical_frame'

        filtered = self._filter(msg)
        self.publisher.publish(filtered)

    def _filter(self, msg: PointCloud2) -> PointCloud2:
        n    = msg.width * msg.height
        step = msg.point_step
        if n == 0 or step == 0 or step % 4 != 0:
            return msg

        offs = {f.name: f.offset for f in msg.fields}
        if not all(k in offs for k in ('x', 'y', 'z')):
            return msg

        floats_per_pt = step // 4
        arr = np.frombuffer(msg.data, dtype=np.float32).copy()
        if len(arr) < n * floats_per_pt:
            return msg
        arr = arr[:n * floats_per_pt].reshape(n, floats_per_pt)

        x = arr[:, offs['x'] // 4]
        y = arr[:, offs['y'] // 4]
        z = arr[:, offs['z'] // 4]   # optical frame: z = forward depth

        # Keep only finite points within MAX_RANGE
        keep = np.isfinite(x) & np.isfinite(y) & np.isfinite(z) & (z < MAX_RANGE) & (z > 0.05)
        arr  = arr[keep]
        x, y, z = x[keep], y[keep], z[keep]

        if len(x) == 0:
            return self._empty_cloud(msg)

        # Voxel-bin neighbour count: assign each point to a 3D bin,
        # keep only points whose bin has >= MIN_NEIGHBOURS occupants.
        bx = (x / FILTER_VOXEL_SIZE).astype(np.int32)
        by = (y / FILTER_VOXEL_SIZE).astype(np.int32)
        bz = (z / FILTER_VOXEL_SIZE).astype(np.int32)

        # Pack 3 int32s into one int64 key for fast counting
        bx -= bx.min(); by -= by.min(); bz -= bz.min()
        dims_y = int(by.max()) + 1
        dims_z = int(bz.max()) + 1
        keys = bx.astype(np.int64) * (dims_y * dims_z) + by.astype(np.int64) * dims_z + bz.astype(np.int64)

        _, inv, counts = np.unique(keys, return_inverse=True, return_counts=True)
        dense = counts[inv] >= MIN_NEIGHBOURS
        arr   = arr[dense]

        if len(arr) == 0:
            return self._empty_cloud(msg)

        out = PointCloud2()
        out.header       = msg.header
        out.height       = 1
        out.width        = len(arr)
        out.fields       = msg.fields
        out.is_bigendian = msg.is_bigendian
        out.point_step   = msg.point_step
        out.row_step     = msg.point_step * len(arr)
        out.is_dense     = True
        out.data         = arr.tobytes()
        return out

    def _empty_cloud(self, msg: PointCloud2) -> PointCloud2:
        out = PointCloud2()
        out.header       = msg.header
        out.height       = 1
        out.width        = 0
        out.fields       = msg.fields
        out.is_bigendian = msg.is_bigendian
        out.point_step   = msg.point_step
        out.row_step     = 0
        out.is_dense     = True
        out.data         = b''
        return out


def main(args=None):
    rclpy.init(args=args)
    node = PointCloudRestamper()
    rclpy.spin(node)
    node.destroy_node()
    rclpy.shutdown()


if __name__ == '__main__':
    main()
