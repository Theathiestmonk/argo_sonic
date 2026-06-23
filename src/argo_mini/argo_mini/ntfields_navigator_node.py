"""
NTFields Navigator Node

Action server that replaces Nav2's SmacPlannerHybrid for global path
planning.  Exposes NavigateToPose on /ntfields/navigate_to_pose.

Flow
----
  1. Receive NavigateToPose goal
  2. Get current pose from /tf  (map → base_link)
  3. Run NTFields gradient-descent planner  (<1 ms after training)
  4. Call FollowPath action on Nav2 controller_server (MPPI)
  5. Stream feedback; return success/failure

Fallback
--------
  If the NTFields model is not yet trained the node falls back to
  Nav2's standard ComputePathToPose + FollowPath pipeline.

Topics watched
--------------
  /ntfields/status  (std_msgs/String)  ← trainer signals READY / TRAINING
  /map              (nav_msgs/OccupancyGrid)  ← kept for re-load on map change
"""

import json
import os
import time
import threading

import numpy as np
import rclpy
from rclpy.node import Node
from rclpy.action import ActionServer, ActionClient, CancelResponse, GoalResponse
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.executors import MultiThreadedExecutor

from geometry_msgs.msg import PoseStamped
from nav_msgs.msg import OccupancyGrid
from nav2_msgs.action import NavigateToPose, FollowPath, ComputePathToPose
from std_msgs.msg import String
import tf2_ros

MODEL_PATH = os.path.expanduser('~/ntfields_model.pt')
META_PATH  = os.path.expanduser('~/ntfields_meta.json')


class NTFieldsNavigatorNode(Node):

    def __init__(self):
        super().__init__('ntfields_navigator')

        self.declare_parameter('device',           'cuda')
        self.declare_parameter('alpha',            0.03)
        self.declare_parameter('goal_radius',      0.12)
        self.declare_parameter('max_steps',        600)
        self.declare_parameter('waypoint_stride',  4)
        self.declare_parameter('global_frame',     'map')
        self.declare_parameter('robot_frame',      'base_link')

        p = self.get_parameter
        self._device         = p('device').value
        self._alpha          = p('alpha').value
        self._goal_radius    = p('goal_radius').value
        self._max_steps      = p('max_steps').value
        self._wp_stride      = p('waypoint_stride').value
        self._global_frame   = p('global_frame').value
        self._robot_frame    = p('robot_frame').value

        self._planner        = None       # NTFieldsPlanner, loaded when ready
        self._model_ready    = False
        self._model_mtime    = 0.0
        self._model_lock     = threading.Lock()

        # TF buffer for robot pose
        self._tf_buffer    = tf2_ros.Buffer()
        self._tf_listener  = tf2_ros.TransformListener(self._tf_buffer, self)

        cb = ReentrantCallbackGroup()

        # Action clients for Nav2 controller
        self._follow_client   = ActionClient(
            self, FollowPath, '/controller_server/follow_path',
            callback_group=cb)
        self._compute_client  = ActionClient(
            self, ComputePathToPose, '/compute_path_to_pose',
            callback_group=cb)

        # Our action server
        self._nav_server = ActionServer(
            self, NavigateToPose,
            '/ntfields/navigate_to_pose',
            execute_callback=self._execute_cb,
            goal_callback=lambda _: GoalResponse.ACCEPT,
            cancel_callback=lambda _: CancelResponse.ACCEPT,
            callback_group=cb,
        )

        # Watch model file and trainer status
        self.create_subscription(String, '/ntfields/status',
                                 self._status_cb, 10)
        self.create_timer(5.0, self._check_model_file)

        self.get_logger().info('NTFields Navigator ready.')

    # ── model loading ─────────────────────────────────────────────────────

    def _status_cb(self, msg: String):
        if msg.data == 'READY':
            self._try_load_model()

    def _check_model_file(self):
        if not os.path.exists(MODEL_PATH):
            return
        mtime = os.path.getmtime(MODEL_PATH)
        if mtime > self._model_mtime:
            self._try_load_model()

    def _try_load_model(self):
        if not (os.path.exists(MODEL_PATH) and os.path.exists(META_PATH)):
            return
        try:
            import torch
            from .ntfields import NTFields2D, NTFieldsPlanner

            device = self._device if torch.cuda.is_available() else 'cpu'
            model  = NTFields2D.load(MODEL_PATH, device=device)

            with open(META_PATH) as f:
                meta = json.load(f)

            with self._model_lock:
                self._planner = NTFieldsPlanner(
                    model, device=device,
                    alpha=self._alpha,
                    goal_radius=self._goal_radius,
                    max_steps=self._max_steps,
                    waypoint_stride=self._wp_stride,
                )
                self._meta         = meta
                self._model_ready  = True
                self._model_mtime  = os.path.getmtime(MODEL_PATH)

            self.get_logger().info(
                f'[NTFields] Model loaded from {MODEL_PATH}  '
                f'({model.num_params():,} params, device={device})')
        except Exception as e:
            self.get_logger().error(f'[NTFields] Model load failed: {e}')

    # ── robot pose ────────────────────────────────────────────────────────

    def _get_robot_xy(self) -> np.ndarray | None:
        try:
            tf = self._tf_buffer.lookup_transform(
                self._global_frame, self._robot_frame, rclpy.time.Time(),
                timeout=rclpy.duration.Duration(seconds=0.5))
            t = tf.transform.translation
            return np.array([t.x, t.y], dtype=np.float32)
        except Exception:
            return None

    # ── action execute ────────────────────────────────────────────────────

    def _execute_cb(self, goal_handle):
        goal: NavigateToPose.Goal = goal_handle.request
        target = goal.pose
        self.get_logger().info(
            f'[NTFields] Navigate to '
            f'({target.pose.position.x:.2f}, {target.pose.position.y:.2f})')

        feedback = NavigateToPose.Feedback()

        # ── NTFields path ──────────────────────────────────────────────

        path_msg = None
        if self._model_ready:
            start_xy = self._get_robot_xy()
            goal_xy  = np.array([target.pose.position.x,
                                  target.pose.position.y], dtype=np.float32)

            if start_xy is not None:
                t0 = time.perf_counter()
                with self._model_lock:
                    path_msg = self._planner.plan_as_ros_path(
                        start_xy, goal_xy,
                        frame_id=self._global_frame,
                        stamp=self.get_clock().now().to_msg(),
                    )
                dt = (time.perf_counter() - t0) * 1000
                self.get_logger().info(
                    f'[NTFields] Path planned in {dt:.1f} ms  '
                    f'({len(path_msg.poses) if path_msg else 0} waypoints)')

        # ── fallback: Nav2 standard planner ───────────────────────────

        if path_msg is None:
            self.get_logger().warn(
                '[NTFields] Model not ready — falling back to Nav2 planner.')
            path_msg = self._nav2_compute_path(goal.pose)

        if path_msg is None or len(path_msg.poses) == 0:
            self.get_logger().error('[NTFields] Path planning failed entirely.')
            goal_handle.abort()
            return NavigateToPose.Result()

        # ── execute via MPPI controller ────────────────────────────────

        success = self._follow_path(goal_handle, path_msg, feedback)

        result = NavigateToPose.Result()
        if success:
            goal_handle.succeed()
        else:
            goal_handle.abort()
        return result

    # ── Nav2 FollowPath action ────────────────────────────────────────────

    def _follow_path(self, goal_handle, path_msg, feedback) -> bool:
        if not self._follow_client.wait_for_server(timeout_sec=5.0):
            self.get_logger().error('FollowPath server not available.')
            return False

        follow_goal = FollowPath.Goal()
        follow_goal.path = path_msg
        follow_goal.controller_id = 'FollowPath'

        future = self._follow_client.send_goal_async(
            follow_goal,
            feedback_callback=lambda fb: self._follow_feedback(fb, goal_handle, feedback),
        )
        rclpy.spin_until_future_complete(self, future, timeout_sec=30.0)

        gh = future.result()
        if gh is None or not gh.accepted:
            return False

        result_future = gh.get_result_async()
        rclpy.spin_until_future_complete(self, result_future, timeout_sec=120.0)

        if goal_handle.is_cancel_requested:
            gh.cancel_goal_async()
            return False

        return result_future.result() is not None

    def _follow_feedback(self, fb_msg, nav_goal_handle, nav_feedback):
        nav_feedback.distance_remaining = fb_msg.feedback.distance_to_goal
        nav_goal_handle.publish_feedback(nav_feedback)

    # ── Nav2 fallback path planning ───────────────────────────────────────

    def _nav2_compute_path(self, target_pose: PoseStamped):
        if not self._compute_client.wait_for_server(timeout_sec=5.0):
            return None

        req = ComputePathToPose.Goal()
        req.goal = target_pose
        req.planner_id = 'GridBased'

        future = self._compute_client.send_goal_async(req)
        rclpy.spin_until_future_complete(self, future, timeout_sec=10.0)
        gh = future.result()
        if gh is None or not gh.accepted:
            return None

        result_future = gh.get_result_async()
        rclpy.spin_until_future_complete(self, result_future, timeout_sec=10.0)
        res = result_future.result()
        if res is None:
            return None
        return res.result.path


def main(args=None):
    rclpy.init(args=args)
    node = NTFieldsNavigatorNode()
    executor = MultiThreadedExecutor(num_threads=4)
    executor.add_node(node)
    try:
        executor.spin()
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    main()
