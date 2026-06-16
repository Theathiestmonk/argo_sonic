#!/usr/bin/env python3
import rclpy
from rclpy.node import Node
from rclpy.qos import qos_profile_sensor_data  # Best Effort sensor profile
from sensor_msgs.msg import PointCloud2

class PointCloudRestamper(Node):
    def __init__(self):
        super().__init__('pointcloud_restamper')
        
        # Subscribe to the camera's Best Effort raw points
        self.subscription = self.create_subscription(
            PointCloud2,
            '/ascamera_hp60c/camera_publisher/depth0/points',
            self.listener_callback,
            qos_profile_sensor_data)
            
        # Publish the corrected points with matching Best Effort QoS
        self.publisher = self.create_publisher(
            PointCloud2,
            '/ascamera_hp60c/camera_publisher/depth0/points_corrected',
            qos_profile_sensor_data)
            
        self.get_logger().info('PointCloud Re-Stamper Active. Synchronizing time & overriding frame to: ascamera_hp60c_color_0')

    def listener_callback(self, msg):
        # 1. Override the hardware timestamp with the active ROS system time
        msg.header.stamp = self.get_clock().now().to_msg()
        
        # 2. OVERRIDE THE FRAME ID: Translate 'camera_hp60c_color_0' to your URDF's 'ascamera_hp60c_color_0'
        msg.header.frame_id = 'ascamera_hp60c_color_0'
        
        self.publisher.publish(msg)

def main(args=None):
    rclpy.init(args=args)
    node = PointCloudRestamper()
    rclpy.spin(node)
    node.destroy_node()
    rclpy.shutdown()

if __name__ == '__main__':
    main()