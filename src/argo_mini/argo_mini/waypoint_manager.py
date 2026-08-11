import rclpy
from rclpy.node import Node
from rclpy.action import ActionClient
from rclpy.qos import QoSProfile, QoSDurabilityPolicy
from geometry_msgs.msg import PoseWithCovarianceStamped, PointStamped
from std_msgs.msg import Int32
from nav_msgs.msg import Odometry
from nav2_msgs.action import NavigateToPose
import json
import os
import threading
import time
import math
from pathlib import Path

# Was hardcoded to ~/argo_mini_ws/src/argo_mini/waypoints — a separate,
# diverged workspace from this repo (see argo_sonic_nav.py's own history
# for the same class of mistake), so this loaded a different, effectively
# empty waypoints file than the one the UI actually reads/writes
# (backend/launcher.py's WAYPOINTS_DIR, <repo_root>/src/argo_mini/waypoints).
# Derived from this file's own location instead — __file__ still resolves
# back through the symlink to the true source location under a
# --symlink-install build (this team's own convention), whether launched
# directly (`python3 .../waypoint_manager.py`) or via `ros2 run`.
DEFAULT_WAYPOINTS_DIR = str(Path(__file__).resolve().parent.parent / 'waypoints')

class WaypointManager(Node):
    def __init__(self):
        super().__init__('waypoint_manager')

        # Each map gets its own waypoints file (<waypoints_dir>/<map_name>.json)
        # so switching maps doesn't mix up tables saved against a different
        # space. Override at launch, e.g.:
        #   ros2 run argo_mini waypoint_manager --ros-args -p map_name:=office_map
        self.declare_parameter('map_name', 'office_map')
        self.declare_parameter('waypoints_dir', DEFAULT_WAYPOINTS_DIR)
        # Nav2 reporting STATUS_SUCCEEDED only guarantees its own goal
        # checker's tolerance was met, which may be loose (or position-only)
        # depending on nav2 params. Verify heading explicitly before we
        # call a waypoint reached: require at least 98% of the full turn
        # circle matched, i.e. actual heading within 2% of 360° of target.
        self.declare_parameter('goal_angle_tolerance_percent', 2.0)
        map_name = self.get_parameter('map_name').value
        waypoints_dir = self.get_parameter('waypoints_dir').value
        goal_angle_tolerance_percent = float(self.get_parameter('goal_angle_tolerance_percent').value)
        self.yaw_tolerance_deg = (goal_angle_tolerance_percent / 100.0) * 360.0
        self.waypoints_file = os.path.join(waypoints_dir, f'{map_name}.json')

        qos = QoSProfile(depth=10, durability=QoSDurabilityPolicy.TRANSIENT_LOCAL)
        
        # Subscribers
        self.pose_sub = self.create_subscription(
            PoseWithCovarianceStamped, '/amcl_pose', self.pose_cb, qos)
        
        self.odom_sub = self.create_subscription(
            Odometry, '/odom', self.odom_cb, 10)
        
        self.click_sub = self.create_subscription(
            PointStamped, '/clicked_point', self.click_cb, 10)
        
        self.dashboard_sub = self.create_subscription(
            Int32, '/dashboard_waypoint_cmd', self.dashboard_cmd_cb, 10)

        # Navigation Action Client
        self.nav_client = ActionClient(self, NavigateToPose, '/navigate_to_pose')
        
        self.current_amcl_pose = None
        self.current_odom_pose = None
        self.clicked_point = None
        self.waypoints = self.load_waypoints()

        self._current_handle = None   # active goal handle (for cancellation)
        self._retry_wp       = None   # waypoint key being retried
        self._stop_retry     = threading.Event()  # set to halt retry loop
        
        self.get_logger().info('Waypoint Manager initialized with improved pose handling.')
        self.print_menu()

    def odom_cb(self, msg):
        """Store latest odometry (good for yaw)"""
        self.current_odom_pose = msg.pose.pose

    def pose_cb(self, msg):
        """AMCL pose in map frame (best for global navigation)"""
        self.current_amcl_pose = msg.pose.pose

    def click_cb(self, msg):
        self.clicked_point = msg.point
        print(f'\n[RViz Click] x={msg.point.x:.3f} y={msg.point.y:.3f}')
        print('> ', end='', flush=True)

    def dashboard_cmd_cb(self, msg):
        wp_id = msg.data
        self.get_logger().info(f"Dashboard requested waypoint {wp_id}")
        threading.Thread(target=self.go_to, args=(wp_id,), daemon=True).start()

    def get_current_pose(self):
        """Return best available pose (AMCL preferred, with good yaw)"""
        if self.current_amcl_pose:
            return self.current_amcl_pose
        return self.current_odom_pose

    def quaternion_to_yaw(self, q):
        """More accurate yaw from quaternion"""
        siny_cosp = 2.0 * (q.w * q.z + q.x * q.y)
        cosy_cosp = 1.0 - 2.0 * (q.y * q.y + q.z * q.z)
        yaw = math.atan2(siny_cosp, cosy_cosp)
        return yaw

    def _orientation_reached(self, n):
        """True if the robot's current heading is within tolerance of the
        waypoint's saved theta. Nav2 reaching the goal position doesn't by
        itself mean the final heading matches what was saved."""
        wp = self.waypoints.get(str(n))
        pose = self.get_current_pose()
        if not wp or not pose:
            return True
        target_yaw = math.radians(float(wp.get('theta', 0.0)))
        actual_yaw = self.quaternion_to_yaw(pose.orientation)
        diff_deg = math.degrees(abs(math.atan2(math.sin(target_yaw - actual_yaw),
                                                 math.cos(target_yaw - actual_yaw))))
        return diff_deg <= self.yaw_tolerance_deg

    def load_waypoints(self):
        if os.path.exists(self.waypoints_file):
            try:
                with open(self.waypoints_file, 'r') as f:
                    data = json.load(f)
                    self.get_logger().info(f'Loaded {len(data)} waypoints from {self.waypoints_file}.')
                    return data
            except Exception as e:
                self.get_logger().error(f'Error loading waypoints: {e}')
        return {}

    def save_waypoints(self):
        os.makedirs(os.path.dirname(self.waypoints_file), exist_ok=True)
        with open(self.waypoints_file, 'w') as f:
            json.dump(self.waypoints, f, indent=2)
        print(f'[Saved] Waypoints updated → {self.waypoints_file}')

    def print_menu(self):
        print('\n' + '='*60)
        print(' ARGO MINI WAYPOINT MANAGER')
        print('='*60)
        print(' s <0-9>  Save current robot pose (AMCL > Odom)')
        print(' c <0-9>  Save clicked point (RViz)')
        print(' g <0-9>  Go to waypoint  (retries until success or cancel)')
        print(' x        Cancel navigation + stop retrying')
        print(' p        Print current pose')
        print(' l        List waypoints')
        print(' q        Quit')
        print('='*60)
        if self.waypoints:
            for k, v in sorted(self.waypoints.items(), key=lambda x: int(x[0])):
                label = 'HOME' if k == '0' else f'Table {k}'
                print(f' {label}: x={float(v["x"]):.3f} y={float(v["y"]):.3f} theta={float(v.get("theta",0)):.1f}°')
        print()

    def save_current_as(self, n):
        pose = self.get_current_pose()
        if not pose:
            print("ERROR: No pose available. Set 2D Pose Estimate in RViz first.")
            return

        key = str(n)
        yaw = self.quaternion_to_yaw(pose.orientation)
        
        self.waypoints[key] = {
            "x": float(pose.position.x),
            "y": float(pose.position.y),
            "qz": float(pose.orientation.z),
            "qw": float(pose.orientation.w),
            "theta": float(math.degrees(yaw))   # for easy reading
        }
        self.save_waypoints()
        print(f"Saved waypoint {n} → x={pose.position.x:.3f}, y={pose.position.y:.3f}, theta={math.degrees(yaw):.1f}°")

    def save_clicked_as(self, n):
        if not self.clicked_point:
            print("ERROR: Click a point in RViz first.")
            return
        # RViz's "Publish Point" only carries x/y/z, no orientation, so
        # there's nothing to read a target heading from. Fall back to the
        # robot's current heading (AMCL > odom) rather than hardcoding
        # identity — still an approximation, not the desired final heading.
        pose = self.get_current_pose()
        if pose:
            yaw = self.quaternion_to_yaw(pose.orientation)
            qz = float(pose.orientation.z)
            qw = float(pose.orientation.w)
        else:
            yaw, qz, qw = 0.0, 0.0, 1.0

        key = str(n)
        self.waypoints[key] = {
            "x": float(self.clicked_point.x),
            "y": float(self.clicked_point.y),
            "qz": qz,
            "qw": qw,
            "theta": float(math.degrees(yaw))
        }
        self.save_waypoints()
        print(f"Saved waypoint {n} from clicked point (x={self.clicked_point.x:.3f}, "
              f"y={self.clicked_point.y:.3f}, theta={math.degrees(yaw):.1f}° from current heading).")

    def cancel_current(self):
        """Cancel the active goal and stop any retry loop."""
        self._stop_retry.set()
        self._retry_wp = None
        if self._current_handle is not None:
            self._current_handle.cancel_goal_async()
            self._current_handle = None
            print("\n[CANCEL] Navigation cancelled.")

    def go_to(self, n, _retry=False):
        """Send a goal to waypoint n.  Set _retry=True for internal retries."""
        key = str(n)
        if key not in self.waypoints:
            print(f"ERROR: Waypoint {n} not found.")
            return

        # A fresh user command cancels any existing retry loop first
        if not _retry:
            self._stop_retry.clear()
            self._retry_wp = key
            if self._current_handle is not None:
                self._current_handle.cancel_goal_async()
                self._current_handle = None
                time.sleep(0.5)   # let Nav2 process the cancel

        if self._stop_retry.is_set():
            return

        wp = self.waypoints[key]
        attempt = getattr(self, '_attempt', 1)
        print(f"\n[NAV] → Waypoint {n}  x={wp['x']:.3f} y={wp['y']:.3f}"
              + (f"  (attempt {attempt})" if attempt > 1 else ""))

        if not self.nav_client.wait_for_server(timeout_sec=8.0):
            print("ERROR: Nav2 action server not available!")
            return

        goal = NavigateToPose.Goal()
        goal.pose.header.frame_id = "map"
        goal.pose.header.stamp = self.get_clock().now().to_msg()
        goal.pose.pose.position.x = float(wp['x'])
        goal.pose.pose.position.y = float(wp['y'])
        goal.pose.pose.orientation.z = float(wp.get('qz', 0.0))
        goal.pose.pose.orientation.w = float(wp.get('qw', 1.0))

        future = self.nav_client.send_goal_async(goal, feedback_callback=self.feedback_cb)
        future.add_done_callback(lambda f: self.goal_response_cb(f, n))

    def feedback_cb(self, feedback_msg):
        dist = feedback_msg.feedback.distance_remaining
        print(f"\r[Progress] Distance remaining: {dist:.2f} m", end='', flush=True)

    def goal_response_cb(self, future, n):
        goal_handle = future.result()
        if not goal_handle.accepted:
            print(f"\n[FAIL] Goal rejected by Nav2.")
            self._schedule_retry(n)
            return
        self._current_handle = goal_handle
        self._attempt = getattr(self, '_attempt', 0) + 1
        print(f"\n[ACCEPTED] Moving toward waypoint {n}...")
        result_future = goal_handle.get_result_async()
        result_future.add_done_callback(lambda f: self.result_cb(f, n))

    def result_cb(self, future, n):
        self._current_handle = None
        result  = future.result()
        status  = result.status

        if status == 4:            # STATUS_SUCCEEDED
            if self._orientation_reached(n):
                self._retry_wp  = None
                self._attempt   = 1
                print(f"\n[SUCCESS] Reached waypoint {n}!")
            elif self._stop_retry.is_set():
                print(f"\n[CANCELLED] Navigation to waypoint {n} stopped.")
            else:
                print(f"\n[FAIL] Position reached but heading off target – retrying waypoint {n}…")
                self._schedule_retry(n)
        elif self._stop_retry.is_set():
            print(f"\n[CANCELLED] Navigation to waypoint {n} stopped.")
        else:
            print(f"\n[FAIL] Status {status} – will retry waypoint {n}…")
            self._schedule_retry(n)

    def _schedule_retry(self, n, delay=2.0):
        """Wait delay seconds then retry, unless stop_retry is set."""
        def _do():
            if not self._stop_retry.wait(timeout=delay):
                threading.Thread(target=self.go_to, args=(n,),
                                 kwargs={'_retry': True}, daemon=True).start()
        threading.Thread(target=_do, daemon=True).start()


def main():
    rclpy.init()
    node = WaypointManager()
    
    spin_thread = threading.Thread(target=rclpy.spin, args=(node,), daemon=True)
    spin_thread.start()

    try:
        while rclpy.ok():
            cmd = input('> ').strip().lower()
            if not cmd:
                continue
            parts = cmd.split()
            
            if parts[0] == 'q':
                node.cancel_current()
                break
            elif parts[0] == 'l':
                node.print_menu()
            elif parts[0] == 'p':
                pose = node.get_current_pose()
                if pose:
                    yaw = node.quaternion_to_yaw(pose.orientation)
                    print(f"Current Pose → x={pose.position.x:.3f}  y={pose.position.y:.3f}  "
                          f"theta={yaw:.3f} rad ({math.degrees(yaw):.1f}°)")
                else:
                    print("No pose available yet.")
            elif parts[0] == 's' and len(parts) == 2:
                node.save_current_as(int(parts[1]))
            elif parts[0] == 'c' and len(parts) == 2:
                node.save_clicked_as(int(parts[1]))
            elif parts[0] == 'g' and len(parts) == 2:
                node._attempt = 1
                threading.Thread(target=node.go_to, args=(int(parts[1]),), daemon=True).start()
            elif parts[0] == 'x':
                node.cancel_current()
            else:
                print("Commands: s <0-9> | c <0-9> | g <0-9> | x | p | l | q")
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    main()
