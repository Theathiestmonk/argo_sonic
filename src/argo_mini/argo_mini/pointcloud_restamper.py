#!/usr/bin/env python3
import rclpy
from rclpy.node import Node
from rclpy.qos import qos_profile_sensor_data
from sensor_msgs.msg import PointCloud2

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
            'PointCloud Re-Stamper Active. '
            'Synchronizing time & overriding frame to: depth_camera_optical_frame')

    def listener_callback(self, msg):
        msg.header.stamp = self.get_clock().now().to_msg()
        # HP60C SDK publishes with frame_id 'ascamera_depth_color_0' which has
        # no TF. depth_camera_optical_frame is the correct URDF optical frame.
        msg.header.frame_id = 'depth_camera_optical_frame'
        self.publisher.publish(msg)

def main(args=None):
    rclpy.init(args=args)
    node = PointCloudRestamper()
    rclpy.spin(node)
    node.destroy_node()
    rclpy.shutdown()

if __name__ == '__main__':
    main()
