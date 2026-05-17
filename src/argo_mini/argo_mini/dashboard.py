#!/usr/bin/env python3

import rclpy
from rclpy.node import Node
from geometry_msgs.msg import PoseStamped, PoseWithCovarianceStamped
from sensor_msgs.msg import BatteryState
from nav_msgs.msg import Odometry
from std_msgs.msg import String
import json
import threading
from http.server import HTTPServer, SimpleHTTPRequestHandler
import os

class DashboardNode(Node):
    def __init__(self):
        super().__init__('dashboard_node')
        
        # Current state
        self.robot_status = "Idle"
        self.current_position = "Base Station"
        self.battery_level = 85.0
        self.connection_status = "Connected"
        self.target_destination = "Base Station"
        self.distance_to_goal = 0.0
        self.motor_status = "OK"
        self.lidar_status = "Active"
        self.imu_status = "Calibrated"
        
        # Waypoints: 0=Base, 1-4=Tables
        self.waypoints = {
            0: {"name": "Base Station", "x": 0.0, "y": 0.0},
            1: {"name": "Table 1", "x": -1.5, "y": 1.5},
            2: {"name": "Table 2", "x": 1.5, "y": 1.5},
            3: {"name": "Table 3", "x": -1.5, "y": -1.5},
            4: {"name": "Table 4", "x": 1.5, "y": -1.5},
        }
        
        # Publishers
        self.goal_pub = self.create_publisher(PoseStamped, '/goal_pose', 10)
        self.command_pub = self.create_publisher(String, '/robot_command', 10)
        
        # Subscribers
        self.create_subscription(PoseWithCovarianceStamped, '/amcl_pose', self.amcl_callback, 10)
        self.create_subscription(Odometry, '/odom', self.odom_callback, 10)
        
        self.get_logger().info("Dashboard Node Started")
    
    def amcl_callback(self, msg):
        # Update current position based on AMCL
        x = msg.pose.pose.position.x
        y = msg.pose.pose.position.y
        
        # Find closest waypoint
        min_dist = float('inf')
        closest_wp = 0
        for wp_id, wp_data in self.waypoints.items():
            dist = ((x - wp_data['x'])**2 + (y - wp_data['y'])**2)**0.5
            if dist < min_dist:
                min_dist = dist
                closest_wp = wp_id
        
        if min_dist < 0.5:
            self.current_position = self.waypoints[closest_wp]["name"]
    
    def odom_callback(self, msg):
        # Update motor status from odometry
        vx = msg.twist.twist.linear.x
        vy = msg.twist.twist.linear.y
        
        if abs(vx) > 0.01 or abs(vy) > 0.01:
            self.motor_status = "OK"
        else:
            self.motor_status = "OK"
    
    def navigate_to_waypoint(self, waypoint_id):
        """Send navigation goal to waypoint"""
        if waypoint_id not in self.waypoints:
            self.get_logger().error(f"Invalid waypoint: {waypoint_id}")
            return
        
        wp = self.waypoints[waypoint_id]
        self.target_destination = wp["name"]
        self.robot_status = "Navigating"
        
        goal = PoseStamped()
        goal.header.frame_id = "map"
        goal.header.stamp = self.get_clock().now().to_msg()
        goal.pose.position.x = float(wp['x'])
        goal.pose.position.y = float(wp['y'])
        goal.pose.orientation.w = 1.0
        
        self.goal_pub.publish(goal)
        self.get_logger().info(f"Navigating to {wp['name']}")
    
    def pause_navigation(self):
        """Pause current navigation"""
        cmd = String()
        cmd.data = "pause"
        self.command_pub.publish(cmd)
        self.robot_status = "Paused"
        self.get_logger().info("Navigation paused")
    
    def cancel_navigation(self):
        """Cancel navigation and return to base"""
        cmd = String()
        cmd.data = "cancel"
        self.command_pub.publish(cmd)
        self.robot_status = "Idle"
        self.target_destination = "Base Station"
        self.get_logger().info("Navigation cancelled")
    
    def emergency_stop(self):
        """Emergency stop - immediate halt"""
        cmd = String()
        cmd.data = "emergency_stop"
        self.command_pub.publish(cmd)
        self.robot_status = "Emergency Stop"
        self.get_logger().warn("EMERGENCY STOP ACTIVATED")
    
    def get_state(self):
        """Return current dashboard state as JSON"""
        return {
            "robotStatus": self.robot_status,
            "currentPosition": self.current_position,
            "batteryLevel": round(self.battery_level, 1),
            "connectionStatus": self.connection_status,
            "targetDestination": self.target_destination,
            "distanceToGoal": round(self.distance_to_goal, 2),
            "motorStatus": self.motor_status,
            "lidarStatus": self.lidar_status,
            "imuStatus": self.imu_status,
            "waypoints": [
                {"id": 0, "name": "Base Station", "x": 0.0, "y": 0.0},
                {"id": 1, "name": "Table 1", "x": -1.5, "y": 1.5},
                {"id": 2, "name": "Table 2", "x": 1.5, "y": 1.5},
                {"id": 3, "name": "Table 3", "x": -1.5, "y": -1.5},
                {"id": 4, "name": "Table 4", "x": 1.5, "y": -1.5},
            ]
        }

dashboard_node = None

class DashboardHTTPHandler(SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/api/state':
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            state = dashboard_node.get_state()
            self.wfile.write(json.dumps(state).encode())
        
        elif self.path == '/':
            self.send_response(200)
            self.send_header('Content-type', 'text/html')
            self.end_headers()
            with open(os.path.join(os.path.dirname(__file__), 'dashboard.html'), 'rb') as f:
                self.wfile.write(f.read())
        
        else:
            self.send_response(404)
            self.end_headers()
    
    def do_POST(self):
        if self.path == '/api/command':
            content_length = int(self.headers['Content-Length'])
            post_data = self.rfile.read(content_length)
            command = json.loads(post_data.decode())
            
            cmd_type = command.get('command')
            
            if cmd_type == 'navigate':
                waypoint_id = command.get('waypoint')
                dashboard_node.navigate_to_waypoint(waypoint_id)
            
            elif cmd_type == 'pause':
                dashboard_node.pause_navigation()
            
            elif cmd_type == 'cancel':
                dashboard_node.cancel_navigation()
            
            elif cmd_type == 'emergency_stop':
                dashboard_node.emergency_stop()
            
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(json.dumps({"status": "ok"}).encode())
        
        else:
            self.send_response(404)
            self.end_headers()
    
    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()
    
    def log_message(self, format, *args):
        pass

def run_http_server():
    """Run HTTP server in background thread"""
    server = HTTPServer(('0.0.0.0', 8080), DashboardHTTPHandler)
    print("Dashboard server running on http://0.0.0.0:8080")
    server.serve_forever()

def main(args=None):
    global dashboard_node
    rclpy.init(args=args)
    dashboard_node = DashboardNode()
    
    # Start HTTP server in background thread
    http_thread = threading.Thread(target=run_http_server, daemon=True)
    http_thread.start()
    
    # Spin the ROS node
    rclpy.spin(dashboard_node)
    dashboard_node.destroy_node()
    rclpy.shutdown()

if __name__ == '__main__':
    main()
