import rclpy
from rclpy.node import Node
from geometry_msgs.msg import Twist, PoseStamped
from sensor_msgs.msg import LaserScan
from tf2_ros import TransformException
from tf2_ros.buffer import Buffer
from tf2_ros.transform_listener import TransformListener

import torch
import numpy as np
import threading
import time

from .model import ActiveNeuralTimeField

class ActiveNeuralPlannerNode(Node):
    def __init__(self):
        super().__init__('active_planner_node')
        
        # TF Buffer and Listener to convert laser scans to map frame
        self.tf_buffer = Buffer()
        self.tf_listener = TransformListener(self.tf_buffer, self)
        
        # ROS 2 Topics
        self.cmd_pub = self.create_publisher(Twist, '/cmd_vel', 10)
        self.scan_sub = self.create_subscription(LaserScan, '/scan', self.scan_callback, 10)
        self.goal_sub = self.create_subscription(PoseStamped, '/goal_pose', self.goal_callback, 10)
        
        # State & Coordinate parameters
        self.robot_pose = np.array([0.0, 0.0])
        self.goal_pose = np.array([0.0, 0.0])
        self.has_goal = False
        self.lock = threading.Lock()
        
        # Active Sliding Window Buffers (Stored directly on GPU)
        self.device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
        self.get_logger().info(f"Targeting compute device: {self.device}")
        
        # Model & Optimization Setup
        self.model = ActiveNeuralTimeField().to(self.device)
        self.optimizer = torch.optim.Adam(self.model.parameters(), lr=1e-3)
        
        # Memory buffer for dynamic obstacles
        self.max_obstacle_points = 2000
        self.obstacle_buffer = torch.zeros((0, 2), device=self.device)
        
        # Continuous PyTorch Thread execution
        self.run_optimization = True
        self.opt_thread = threading.Thread(target=self.optimization_thread_loop)
        self.opt_thread.daemon = True
        self.opt_thread.start()
        
        # Local controller timer (20 Hz)
        self.control_timer = self.create_timer(0.05, self.control_loop)
        self.get_logger().info("Active Neural Planner successfully initialized.")

    def goal_callback(self, msg):
        with self.lock:
            self.goal_pose[0] = msg.pose.position.x
            self.goal_pose[1] = msg.pose.position.y
            self.has_goal = True
            # Clear historical obstacle memory when setting a new goal
            self.obstacle_buffer = torch.zeros((0, 2), device=self.device)
            self.get_logger().info(f"Active planning goal modified: x={self.goal_pose[0]:.2f}, y={self.goal_pose[1]:.2f}")

    def update_robot_pose(self):
        try:
            transform = self.tf_buffer.lookup_transform('map', 'base_link', rclpy.time.Time())
            self.robot_pose[0] = transform.transform.translation.x
            self.robot_pose[1] = transform.transform.translation.y
            return True
        except TransformException:
            return False

    def scan_callback(self, msg):
        """
        Projects polar LiDAR ranges into 2D Cartesian world coordinates using TF.
        """
        if not self.has_goal:
            return

        try:
            # Lookup sensor transform
            transform = self.tf_buffer.lookup_transform('map', msg.header.frame_id, msg.header.stamp)
        except TransformException:
            return

        angles = np.arange(msg.angle_min, msg.angle_max, msg.angle_increment)
        ranges = np.array(msg.ranges)
        
        # Mask out invalid reading values
        valid_indices = (ranges > msg.range_min) & (ranges < msg.range_max)
        ranges = ranges[valid_indices]
        angles = angles[valid_indices]

        # Convert Polar to Cartesian (in sensor local frame)
        xs = ranges * np.cos(angles)
        ys = ranges * np.sin(angles)
        
        tx = transform.transform.translation.x
        ty = transform.transform.translation.y
        
        # Extract Euler rotation around Z (yaw)
        q = transform.transform.rotation
        siny_cosp = 2 * (q.w * q.z + q.x * q.y)
        cosy_cosp = 1 - 2 * (q.y * q.y + q.z * q.z)
        yaw = np.arctan2(siny_cosp, cosy_cosp)

        # Apply transformation matrix manually
        cos_yaw = np.cos(yaw)
        sin_yaw = np.sin(yaw)
        
        global_xs = xs * cos_yaw - ys * sin_yaw + tx
        global_ys = xs * sin_yaw + ys * cosy_cosp + ty
        
        new_obstacles = np.stack([global_xs, global_ys], axis=1)
        new_obstacles_tensor = torch.tensor(new_obstacles, dtype=torch.float32, device=self.device)

        with self.lock:
            # Append to buffer and enforce size limits
            self.obstacle_buffer = torch.cat([self.obstacle_buffer, new_obstacles_tensor], dim=0)
            if self.obstacle_buffer.shape[0] > self.max_obstacle_points:
                self.obstacle_buffer = self.obstacle_buffer[-self.max_obstacle_points:]

    def sample_active_batch(self, batch_size=256):
        """
        Synthesizes both obstacle data and free-space points relative to target destination.
        """
        if self.obstacle_buffer.shape[0] == 0:
            return None, None

        half_batch = batch_size // 2

        with self.lock:
            # Sample obstacle coordinates
            indices = torch.randint(0, self.obstacle_buffer.shape[0], (half_batch,))
            obs_samples = self.obstacle_buffer[indices]
            
            # Shift samples relative to target coordinate
            goal_tensor = torch.tensor(self.goal_pose, dtype=torch.float32, device=self.device)
            obs_rel = obs_samples - goal_tensor
            
            # Sample random free-space points around the robot's active workspace
            robot_tensor = torch.tensor(self.robot_pose, dtype=torch.float32, device=self.device)
            free_rel = (torch.rand((half_batch, 2), device=self.device) - 0.5) * 10.0 + (robot_tensor - goal_tensor)

        # Combine samples
        coords = torch.cat([obs_rel, free_rel], dim=0)
        
        # Assign speed maps (0.01 for obstacles, 1.0 for open floor space)
        speeds_obs = torch.full((half_batch, 1), 0.01, device=self.device)
        speeds_free = torch.full((half_batch, 1), 1.0, device=self.device)
        speeds = torch.cat([speeds_obs, speeds_free], dim=0)

        return coords, speeds

    def optimization_thread_loop(self):
        """
        High-frequency neural optimization running on your GPU.
        """
        # --- GPU Context Warmup Step ---
        if torch.cuda.is_available():
            try:
                dummy_coords = torch.zeros((1, 2), device=self.device)
                with self.lock:
                    _ = self.model(dummy_coords)
                self.get_logger().info("GPU Thread Warmup Successful. CUDA Context Initialized.")
            except Exception as e:
                self.get_logger().warn(f"GPU Thread Warmup Warning: {e}")

        while self.run_optimization and rclpy.ok():
            if not self.has_goal or self.obstacle_buffer.shape[0] == 0:
                time.sleep(0.05)
                continue

            # Thread-safe forward, backward, and optimization sequence
            with self.lock:
                coords, speeds = self.sample_active_batch()
                if coords is None:
                    continue

                coords.requires_grad_(True)
                self.optimizer.zero_grad()

                predictions = self.model(coords)

                # Spatial derivative calculations
                gradients = torch.autograd.grad(
                    outputs=predictions,
                    inputs=coords,
                    grad_outputs=torch.ones_like(predictions),
                    create_graph=True,
                    retain_graph=True,
                    only_inputs=True
                )[0]

                # Eikonal Objective: ||grad(T)|| = 1 / Speed
                grad_norm = torch.norm(gradients, dim=-1, keepdim=True)
                eikonal_loss = torch.mean((grad_norm - (1.0 / speeds)) ** 2)

                # Target convergence constraints: T(Goal) = 0
                origin = torch.zeros((1, 2), device=self.device)
                boundary_loss = torch.mean(self.model(origin) ** 2)

                total_loss = eikonal_loss + 15.0 * boundary_loss
                total_loss.backward()
                self.optimizer.step()

            # Slight yield to prevent thread lockups
            time.sleep(0.002)

    def control_loop(self):
        if not self.has_goal or not self.update_robot_pose():
            return

        # Thread-safe forward pass and spatial autograd calculations
        with self.lock:
            rel_x = self.robot_pose[0] - self.goal_pose[0]
            rel_y = self.robot_pose[1] - self.goal_pose[1]

            # Calculate control instructions from the time field gradient
            robot_coords = torch.tensor([[rel_x, rel_y]], dtype=torch.float32, device=self.device, requires_grad=True)
            t_val = self.model(robot_coords)

            gradient = torch.autograd.grad(
                outputs=t_val,
                inputs=robot_coords,
                grad_outputs=torch.ones_like(t_val),
                only_inputs=True
            )[0].cpu().detach().numpy()[0]

        # Calculate Euclidean distance to destination
        distance = np.hypot(rel_x, rel_y)
        if distance < 0.25:
            self.has_goal = False
            self.stop_robot()
            self.get_logger().info("Target reached successfully.")
            return

        direction = -gradient
        norm = np.linalg.norm(direction)
        if norm > 1e-4:
            direction /= norm

        # Map to Twist message output
        cmd = Twist()
        target_heading = np.arctan2(direction[1], direction[0])
        
        cmd.linear.x = 0.18 # Safe forward velocity (meters per second)
        cmd.angular.z = target_heading * 0.45 # Scaling angular gain
        
        self.cmd_pub.publish(cmd)

    def stop_robot(self):
        # Safety constraint: Only attempt to publish if context is alive
        if rclpy.ok():
            try:
                cmd = Twist()
                self.cmd_pub.publish(cmd)
            except Exception:
                pass

def main(args=None):
    rclpy.init(args=args)
    node = ActiveNeuralPlannerNode()
    try:
        rclpy.spin(node)
    except (KeyboardInterrupt, rclpy.executors.ExternalShutdownException):
        pass
    finally:
        node.run_optimization = False
        
        # Verify ROS context remains active before stopping robot
        if rclpy.ok():
            node.stop_robot()
            
        node.destroy_node()
        
        # Safely shut down ROS context
        if rclpy.ok():
            rclpy.shutdown()