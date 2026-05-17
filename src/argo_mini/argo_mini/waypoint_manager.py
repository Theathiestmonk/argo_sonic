import rclpy
from rclpy.node import Node
from rclpy.action import ActionClient
from rclpy.qos import QoSProfile, QoSDurabilityPolicy
from geometry_msgs.msg import PoseWithCovarianceStamped, PointStamped
from nav2_msgs.action import NavigateToPose
import json
import os
import threading

WAYPOINTS_FILE = os.path.expanduser(
    '~/argo_mini_ws/src/argo_mini/waypoints/waypoints.json')


class WaypointManager(Node):
    def __init__(self):
        super().__init__('waypoint_manager')

        qos = QoSProfile(depth=10,
                         durability=QoSDurabilityPolicy.TRANSIENT_LOCAL)

        self.pose_sub = self.create_subscription(
            PoseWithCovarianceStamped, '/amcl_pose', self.pose_cb, qos)
        self.click_sub = self.create_subscription(
            PointStamped, '/clicked_point', self.click_cb, 10)
        self.nav_client = ActionClient(
            self, NavigateToPose, '/navigate_to_pose')

        self.current_pose  = None
        self.clicked_point = None
        self.waypoints     = self.load_waypoints()
        self.get_logger().info('Waypoint Manager ready.')
        self.print_menu()

    def pose_cb(self, msg):
        self.current_pose = msg.pose.pose

    def click_cb(self, msg):
        self.clicked_point = msg.point
        print(f'\nClicked: x={msg.point.x:.3f} y={msg.point.y:.3f}')
        print('> ', end='', flush=True)

    def load_waypoints(self):
        if os.path.exists(WAYPOINTS_FILE):
            with open(WAYPOINTS_FILE, 'r') as f:
                wp = json.load(f)
                self.get_logger().info(f'Loaded {len(wp)} waypoints.')
                return wp
        return {}

    def save_waypoints(self):
        os.makedirs(os.path.dirname(WAYPOINTS_FILE), exist_ok=True)
        with open(WAYPOINTS_FILE, 'w') as f:
            json.dump(self.waypoints, f, indent=2)

    def print_menu(self):
        print('\n' + '='*50)
        print('  ARGO MINI WAYPOINT MANAGER')
        print('='*50)
        print('  s <0-9>   Save robot position as waypoint')
        print('  c <0-9>   Save clicked map point as waypoint')
        print('  g <0-9>   Go to waypoint')
        print('  l         List all waypoints')
        print('  q         Quit')
        print('='*50)
        for k, v in self.waypoints.items():
            label = 'HOME' if k == '0' else f'Waypoint {k}'
            print(f'  {label}: x={v["x"]:.2f} y={v["y"]:.2f}')
        print()

    def save_current_as(self, number):
        if self.current_pose is None:
            print('ERROR: No pose. Set 2D Pose Estimate in RViz first.')
            return
        key = str(number)
        self.waypoints[key] = {
            'x':  self.current_pose.position.x,
            'y':  self.current_pose.position.y,
            'qz': self.current_pose.orientation.z,
            'qw': self.current_pose.orientation.w,
        }
        self.save_waypoints()
        print(f'Saved waypoint {number}: '
              f'x={self.waypoints[key]["x"]:.3f} '
              f'y={self.waypoints[key]["y"]:.3f}')

    def save_clicked_as(self, number):
        if self.clicked_point is None:
            print('ERROR: No clicked point. Use Publish Point in RViz.')
            return
        key = str(number)
        self.waypoints[key] = {
            'x':  self.clicked_point.x,
            'y':  self.clicked_point.y,
            'qz': 0.0,
            'qw': 1.0,
        }
        self.save_waypoints()
        print(f'Saved waypoint {number} from click: '
              f'x={self.waypoints[key]["x"]:.3f} '
              f'y={self.waypoints[key]["y"]:.3f}')

    def go_to(self, number):
        key = str(number)
        if key not in self.waypoints:
            print(f'ERROR: Waypoint {number} not saved yet.')
            return
        wp = self.waypoints[key]
        print(f'Waiting for Nav2...')
        if not self.nav_client.wait_for_server(timeout_sec=10.0):
            print('ERROR: Nav2 action server not available.')
            return
        print(f'Going to waypoint {number}: x={wp["x"]:.3f} y={wp["y"]:.3f}')
        goal = NavigateToPose.Goal()
        goal.pose.header.frame_id    = 'map'
        goal.pose.header.stamp       = self.get_clock().now().to_msg()
        goal.pose.pose.position.x    = wp['x']
        goal.pose.pose.position.y    = wp['y']
        goal.pose.pose.position.z    = 0.0
        goal.pose.pose.orientation.x = 0.0
        goal.pose.pose.orientation.y = 0.0
        goal.pose.pose.orientation.z = wp['qz']
        goal.pose.pose.orientation.w = wp['qw']
        future = self.nav_client.send_goal_async(
            goal, feedback_callback=self.feedback_cb)
        future.add_done_callback(
            lambda f: self.goal_response_cb(f, number))

    def feedback_cb(self, feedback):
        dist = feedback.feedback.distance_remaining
        print(f'\r  Distance remaining: {dist:.2f}m    ',
              end='', flush=True)

    def goal_response_cb(self, future, number):
        handle = future.result()
        if not handle.accepted:
            print(f'\nGoal REJECTED.')
            return
        print(f'\nMoving to waypoint {number}...')
        handle.get_result_async().add_done_callback(
            lambda f: self.result_cb(f, number))

    def result_cb(self, future, number):
        print(f'\nReached waypoint {number}!')
        print('> ', end='', flush=True)


def main():
    rclpy.init()
    node = WaypointManager()
    threading.Thread(
        target=rclpy.spin, args=(node,), daemon=True).start()
    try:
        while True:
            cmd = input('> ').strip().lower()
            if not cmd:
                continue
            parts = cmd.split()
            if parts[0] == 'q':
                break
            elif parts[0] == 'l':
                if not node.waypoints:
                    print('No waypoints saved.')
                else:
                    for k, v in node.waypoints.items():
                        label = 'HOME' if k == '0' else f'Waypoint {k}'
                        print(f'  {label}: x={v["x"]:.3f} y={v["y"]:.3f}')
            elif parts[0] == 's' and len(parts) == 2:
                n = int(parts[1])
                if 0 <= n <= 9:
                    node.save_current_as(n)
                else:
                    print('Use 0-9 only.')
            elif parts[0] == 'c' and len(parts) == 2:
                n = int(parts[1])
                if 0 <= n <= 9:
                    node.save_clicked_as(n)
                else:
                    print('Use 0-9 only.')
            elif parts[0] == 'g' and len(parts) == 2:
                n = int(parts[1])
                if 0 <= n <= 9:
                    node.go_to(n)
                else:
                    print('Use 0-9 only.')
            else:
                print('Commands: s <0-9> | c <0-9> | g <0-9> | l | q')
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    main()
