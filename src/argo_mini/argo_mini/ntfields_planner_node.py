#!/usr/bin/env python3
"""
Mode 1: NTFields Global Planner Node.

Lifecycle node that replaces Nav2's planner_server.
Advertises the same ComputePathToPose action so the BT Navigator works
without any change to Nav2's core stack.

Node name: planner_server  (matches lifecycle manager expectations)
Action:    /compute_path_to_pose  (same as nav2_planner)
"""

import os
import time

import numpy as np
import rclpy
from rclpy.lifecycle import LifecycleNode, TransitionCallbackReturn, State
from rclpy.action import ActionServer, CancelResponse, GoalResponse
from rclpy.executors import MultiThreadedExecutor
from geometry_msgs.msg import PoseStamped
from nav_msgs.msg import Path
from nav2_msgs.action import ComputePathToPose
import tf2_ros
from tf2_ros import TransformException

from .ntfields_model import NTFieldsModel, CoordNormalizer


def _chaikin_smooth(points: list, iterations: int = 3) -> list:
    """Chaikin's corner-cutting: replaces each interior corner with two
    points 1/4 and 3/4 along its adjoining segments, repeated a few times
    to converge toward a smooth curve. predict_path()'s fixed-step
    gradient descent (~0.30m/step, see ntfields.yaml) locks in direction
    per step and only bends sharply right at a learned obstacle boundary
    — this turns that "polygon" path into one that curves through turns
    instead of cornering through them.

    Deliberately geometric, not obstacle-aware: each new point is a
    weighted average of two existing points, so it can never land outside
    the original polyline's own convex hull — it rounds corners without
    ever bulging past the space the original (already obstacle-avoiding)
    path swept out. Endpoints are kept exact — start is the robot's real
    pose and goal is the exact requested destination, not approximations.
    """
    if len(points) < 3:
        return points
    for _ in range(iterations):
        smoothed = [points[0]]
        for p0, p1 in zip(points[:-1], points[1:]):
            smoothed.append((0.75 * p0[0] + 0.25 * p1[0], 0.75 * p0[1] + 0.25 * p1[1]))
            smoothed.append((0.25 * p0[0] + 0.75 * p1[0], 0.25 * p0[1] + 0.75 * p1[1]))
        smoothed.append(points[-1])
        points = smoothed
    return points


class NTFieldsPlannerNode(LifecycleNode):
    """
    Drop-in Python replacement for nav2_planner/planner_server.

    On activate: loads the pre-trained NTFields model and opens the
    ComputePathToPose action server. All path requests are answered via
    bidirectional gradient descent; typical latency < 30 ms on Jetson Orin.
    """

    def __init__(self):
        # Same node name as planner_server so lifecycle manager calls work
        super().__init__('planner_server')

    # ── configure ─────────────────────────────────────────────────────────────

    def on_configure(self, state: State) -> TransitionCallbackReturn:
        self.declare_parameter('model_path', '')
        self.declare_parameter('device',     'cuda')
        self.declare_parameter('step_size',  0.015)
        self.declare_parameter('tol',        0.025)
        self.declare_parameter('max_iter',   400)

        self._model_path = os.path.expanduser(
            self.get_parameter('model_path').value)
        self._device     = self.get_parameter('device').value
        self._step_size  = self.get_parameter('step_size').value
        self._tol        = self.get_parameter('tol').value
        self._max_iter   = int(self.get_parameter('max_iter').value)

        self._model      = None
        self._norm       = None
        self._tf_buf     = tf2_ros.Buffer()
        self._tf_listen  = tf2_ros.TransformListener(self._tf_buf, self)
        # Republishes each computed path on a plain topic, purely for
        # visualization (RViz's usual "Path" display, or the web UI's
        # MapCanvas.jsx via rosbridge) — ComputePathToPose's own result
        # only reaches bt_navigator, nothing external ever sees it otherwise.
        self._plan_pub   = self.create_publisher(Path, 'plan', 10)

        # Load model at configure time so on_activate is instant
        if not self._model_path or not os.path.exists(self._model_path):
            self.get_logger().error(
                f'[NTFieldsPlanner] model not found: {self._model_path}')
            return TransitionCallbackReturn.FAILURE

        t0 = time.time()
        self.get_logger().info(f'[NTFieldsPlanner] loading {self._model_path}…')
        self._model = NTFieldsModel(dim=2, device=self._device)
        self._norm  = self._model.load(self._model_path)

        if self._norm is None:
            self.get_logger().error(
                '[NTFieldsPlanner] model has no CoordNormalizer – '
                'retrain with ntfields_offline_train.py')
            return TransitionCallbackReturn.FAILURE

        self.get_logger().info(
            f'[NTFieldsPlanner] configured  model loaded in {time.time()-t0:.2f}s  '
            f'epoch={self._model.epoch}  scale={self._norm.scale:.2f}m  '
            f'device={self._device}')
        return TransitionCallbackReturn.SUCCESS

    # ── activate ──────────────────────────────────────────────────────────────

    def on_activate(self, state: State) -> TransitionCallbackReturn:
        if self._model is None:
            self.get_logger().error('[NTFieldsPlanner] model not loaded – configure first')
            return TransitionCallbackReturn.FAILURE

        self._action_server = ActionServer(
            self,
            ComputePathToPose,
            'compute_path_to_pose',
            execute_callback = self._execute_cb,
            goal_callback    = lambda _req: GoalResponse.ACCEPT,
            cancel_callback  = lambda _req: CancelResponse.ACCEPT,
        )
        self.get_logger().info('[NTFieldsPlanner] ACTIVE – ready for ComputePathToPose')
        return TransitionCallbackReturn.SUCCESS

    # ── deactivate / cleanup ───────────────────────────────────────────────────

    def on_deactivate(self, state: State) -> TransitionCallbackReturn:
        if hasattr(self, '_action_server'):
            self._action_server.destroy()
        return TransitionCallbackReturn.SUCCESS

    def on_cleanup(self, state: State) -> TransitionCallbackReturn:
        self._model = None
        self._norm  = None
        return TransitionCallbackReturn.SUCCESS

    def on_shutdown(self, state: State) -> TransitionCallbackReturn:
        return TransitionCallbackReturn.SUCCESS

    # ── Action callback ────────────────────────────────────────────────────────

    def _execute_cb(self, goal_handle):
        req    = goal_handle.request
        result = ComputePathToPose.Result()
        t0     = time.time()

        goal_xy = np.array([req.goal.pose.position.x,
                             req.goal.pose.position.y], dtype=np.float32)

        self.get_logger().info(
            f'[NTFieldsPlanner] plan to ({goal_xy[0]:.2f}, {goal_xy[1]:.2f})')

        # Start pose
        if req.use_start:
            start_xy = np.array([req.start.pose.position.x,
                                  req.start.pose.position.y], dtype=np.float32)
        else:
            start_xy = self._robot_xy()
            if start_xy is None:
                self.get_logger().error('[NTFieldsPlanner] TF lookup failed')
                goal_handle.abort()
                return result

        # Domain check
        if not self._in_domain(start_xy) or not self._in_domain(goal_xy):
            self.get_logger().warn(
                '[NTFieldsPlanner] start or goal outside map domain')
            goal_handle.abort()
            return result

        # Normalise → model space
        s_n = self._norm.to_model(start_xy)
        g_n = self._norm.to_model(goal_xy)

        # Gradient-descent path
        path_n = self._model.predict_path(
            s_n, g_n,
            step_size = self._step_size,
            tol       = self._tol,
            max_iter  = self._max_iter,
        )

        if path_n is None or len(path_n) < 2:
            self.get_logger().warn('[NTFieldsPlanner] predict_path returned empty path')
            goal_handle.abort()
            return result

        # World-space points, then smoothed — predict_path()'s fixed-step
        # gradient descent produces a "polygon" (locks in direction for
        # each ~0.30m step, only bends sharply at a learned obstacle
        # boundary); Chaikin smoothing rounds those corners into a curve
        # MPPI can track without the sharp heading snaps. Smoothed in
        # world space (metres) rather than normalised model space — purely
        # geometric, so it doesn't matter which, but keeps this step
        # decoupled from the model's own coordinate handling.
        world_pts = [tuple(self._norm.to_world(pt_n)) for pt_n in path_n]
        smoothed_pts = _chaikin_smooth(world_pts)

        # Build nav_msgs/Path
        path_msg              = Path()
        path_msg.header.frame_id = 'map'
        path_msg.header.stamp    = self.get_clock().now().to_msg()

        for x, y in smoothed_pts:
            pose  = PoseStamped()
            pose.header            = path_msg.header
            pose.pose.position.x   = float(x)
            pose.pose.position.y   = float(y)
            pose.pose.position.z   = 0.0
            pose.pose.orientation.w = 1.0
            path_msg.poses.append(pose)

        elapsed = time.time() - t0
        self.get_logger().info(
            f'[NTFieldsPlanner] path ready  '
            f'waypoints={len(path_n)}->{len(smoothed_pts)} (smoothed)  t={elapsed*1000:.1f}ms')

        self._plan_pub.publish(path_msg)
        result.path = path_msg
        result.planning_time.sec     = int(elapsed)
        result.planning_time.nanosec = int((elapsed % 1.0) * 1e9)
        goal_handle.succeed()
        return result

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _robot_xy(self):
        try:
            t = self._tf_buf.lookup_transform(
                'map', 'base_link', rclpy.time.Time())
            return np.array([t.transform.translation.x,
                             t.transform.translation.y], dtype=np.float32)
        except TransformException:
            return None

    def _in_domain(self, xy: np.ndarray) -> bool:
        n = self._norm.to_model(xy)
        return bool(np.all(np.abs(n) <= 0.60))   # slight margin beyond ±0.5


def main(args=None):
    rclpy.init(args=args)
    node = NTFieldsPlannerNode()
    executor = MultiThreadedExecutor()
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
