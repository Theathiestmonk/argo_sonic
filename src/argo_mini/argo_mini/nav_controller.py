#!/usr/bin/env python3
"""
Simplified Navigation Controller for Argo Mini
- Auto-initializes pose at kitchen on startup
- Accepts waypoint goals via /dashboard_waypoint_cmd
- Auto-returns to kitchen 10 seconds after reaching goal
"""

import json
import math
import time
import threading
from pathlib import Path

import rclpy
from rclpy.node import Node
from rclpy.action import ActionClient
from geometry_msgs.msg import PoseWithCovarianceStamped, PoseStamped
from nav2_msgs.action import NavigateToPose
from std_msgs.msg import Int32
from nav_msgs.msg import Odometry


class NavController(Node):
    """Navigation controller with auto-return logic."""

    def __init__(self):
        super().__init__('nav_controller')

        # Load waypoints from map file
        waypoint_file = Path(__file__).parent.parent / "waypoints" / "office_map.json"
        with open(waypoint_file) as f:
            self.waypoints = json.load(f)

        self.get_logger().info(f'Loaded {len(self.waypoints)} waypoints from {waypoint_file.name}')

        # Find kitchen dynamically by searching for "name": "Kitchen"
        self.kitchen_wp_id = None
        self.kitchen_wp = None
        for wp_id, wp_data in self.waypoints.items():
            if wp_data.get("name") == "Kitchen":
                self.kitchen_wp_id = wp_id
                self.kitchen_wp = wp_data
                break

        if not self.kitchen_wp:
            self.get_logger().error('ERROR: No waypoint with name="Kitchen" found in map JSON!')
            raise RuntimeError('Kitchen waypoint not found in office_map.json')

        self.get_logger().info(
            f'Kitchen found at waypoint {self.kitchen_wp_id}: '
            f'x={self.kitchen_wp["x"]:.3f}, y={self.kitchen_wp["y"]:.3f}'
        )

        # Nav2 client
        self.nav_client = ActionClient(self, NavigateToPose, 'navigate_to_pose')
        self.get_logger().info('Waiting for nav2 server...')
        self.nav_client.wait_for_server()
        self.get_logger().info('Nav2 server ready')

        # Publishers
        self.init_pose_pub = self.create_publisher(
            PoseWithCovarianceStamped, '/initialpose', 10
        )

        # Subscribers
        self.goal_sub = self.create_subscription(
            Int32, '/dashboard_waypoint_cmd', self.on_goal_cmd, 10
        )
        self.odom_sub = self.create_subscription(
            Odometry, '/odom', self.on_odom, 10
        )

        # State
        self.current_goal_wp = None
        self.goal_handle = None
        self.robot_x = 0.0
        self.robot_y = 0.0
        self.state_lock = threading.Lock()

        # Auto-initialize pose at kitchen
        time.sleep(1.0)  # wait for publishers to connect
        self.set_initial_pose(self.kitchen_wp['x'], self.kitchen_wp['y'])
        self.get_logger().info('✓ Initial pose set to kitchen')

    def set_initial_pose(self, x: float, y: float, theta: float = 0.0):
        """Publish initial pose to /initialpose."""
        msg = PoseWithCovarianceStamped()
        msg.header.frame_id = 'map'
        msg.header.stamp = self.get_clock().now().to_msg()

        msg.pose.pose.position.x = x
        msg.pose.pose.position.y = y
        msg.pose.pose.position.z = 0.0

        # Convert theta to quaternion
        msg.pose.pose.orientation.z = math.sin(theta / 2.0)
        msg.pose.pose.orientation.w = math.cos(theta / 2.0)
        msg.pose.pose.orientation.x = 0.0
        msg.pose.pose.orientation.y = 0.0

        # Covariance
        msg.pose.covariance[0] = 0.25   # x
        msg.pose.covariance[7] = 0.25   # y
        msg.pose.covariance[35] = 0.1   # theta

        self.init_pose_pub.publish(msg)

    def on_odom(self, msg: Odometry):
        """Track robot position."""
        with self.state_lock:
            self.robot_x = msg.pose.pose.position.x
            self.robot_y = msg.pose.pose.position.y

    def on_goal_cmd(self, msg: Int32):
        """Handle waypoint goal command."""
        wp_id = str(msg.data)

        if wp_id not in self.waypoints:
            self.get_logger().warn(f'Invalid waypoint: {wp_id}')
            return

        wp = self.waypoints[wp_id]
        self.get_logger().info(
            f'→ Goal: waypoint {wp_id} at ({wp["x"]:.2f}, {wp["y"]:.2f})'
        )

        with self.state_lock:
            self.current_goal_wp = wp_id

        # Send goal to nav2
        self.send_goal_to_nav2(wp['x'], wp['y'])

    def send_goal_to_nav2(self, x: float, y: float):
        """Send navigation goal to nav2."""
        goal = NavigateToPose.Goal()
        goal.pose.header.frame_id = 'map'
        goal.pose.header.stamp = self.get_clock().now().to_msg()
        goal.pose.pose.position.x = x
        goal.pose.pose.position.y = y
        goal.pose.pose.orientation.w = 1.0  # face forward

        # Cancel previous goal if any
        if self.goal_handle:
            self.nav_client.cancel_goal_async(self.goal_handle)

        self.goal_handle = self.nav_client.send_goal_async(
            goal,
            feedback_callback=self.on_nav_feedback,
            done_callback=self.on_nav_done
        )

    def on_nav_feedback(self, msg):
        """Called while navigating (100Hz feedback from nav2)."""
        # msg.feedback.current_pose has live position
        pass

    def on_nav_done(self, future):
        """Called when goal completes."""
        result = future.result()

        if result.status == 4:  # SUCCEEDED
            self.get_logger().info('✓ Goal reached!')

            with self.state_lock:
                current_goal = self.current_goal_wp

            # If goal was NOT kitchen, auto-return
            if current_goal != self.kitchen_wp_id:
                self.get_logger().info(f'⏱ Waiting 10 seconds before returning to kitchen...')
                threading.Timer(10.0, self.return_to_kitchen).start()
            else:
                self.get_logger().info('✓ Back at kitchen')
        else:
            self.get_logger().warn(f'Goal failed with status {result.status}')

    def return_to_kitchen(self):
        """Auto-return to kitchen (waypoint 0)."""
        self.get_logger().info('→ Returning to kitchen...')
        self.send_goal_to_nav2(
            self.kitchen_wp['x'],
            self.kitchen_wp['y']
        )


def main(args=None):
    rclpy.init(args=args)
    node = NavController()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    main()
