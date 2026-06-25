#!/usr/bin/env python3
"""
Frontier Exploration — Argo Mini
==================================
Autonomously explores unknown space during a SLAM mapping session.

Algorithm:
  1. Subscribe to /map (OccupancyGrid published by SLAM Toolbox)
  2. Detect frontier cells — free cells (0–25) adjacent to unknown cells (−1)
  3. Cluster adjacent frontier cells via BFS
  4. Score each cluster: score = size / sqrt(distance_from_robot)
  5. Send the highest-scoring cluster centroid to Nav2 NavigateToPose
  6. On success/failure/timeout → pick the next frontier
  7. When no frontiers remain → exploration complete, print save instructions

Requirements:
  - SLAM Toolbox running in async (mapping) mode  → provides /map
  - Nav2 stack running (bt_navigator, planner_server, controller_server, …)
  - TF: map → odom → base_link chain must be live

Topics:
  Sub:  /map                  (nav_msgs/OccupancyGrid)
  Pub:  /frontier_markers     (visualization_msgs/MarkerArray) — for RViz
  Act:  navigate_to_pose      (nav2_msgs/action/NavigateToPose)
"""

import collections
import math
import threading

import numpy as np
import rclpy
from action_msgs.msg import GoalStatus
from geometry_msgs.msg import PoseStamped
from nav2_msgs.action import NavigateToPose
from nav_msgs.msg import OccupancyGrid
from rclpy.action import ActionClient
from rclpy.duration import Duration
from rclpy.node import Node
from visualization_msgs.msg import Marker, MarkerArray

import tf2_ros
from tf2_ros import TransformException


class FrontierExplorer(Node):
    """
    Frontier-based autonomous exploration node for Argo Mini.

    State machine:
        IDLE        — waiting for the first /map message
        EXPLORING   — selecting and navigating to frontiers
        NAVIGATING  — Nav2 goal is active
        DONE        — no frontiers remain
    """

    # ── Default parameter values ───────────────────────────────────────────────
    _FREE_THRESH_DEF    = 25     # occupancy ≤ this → free
    _MIN_FRONTIER_DEF   = 8     # cells — clusters smaller than this are ignored
    _GOAL_TOL_DEF       = 0.40  # m   — if robot is within this of goal, move on
    _NAV_TIMEOUT_DEF    = 60.0  # s   — cancel goal if not reached in this time
    _UPDATE_RATE_DEF    = 1.0   # Hz  — how often to look for new frontiers
    _BL_RADIUS_DEF      = 0.35  # m   — radius around a failed goal to blacklist

    def __init__(self):
        super().__init__('frontier_explorer')

        # ── Parameters ────────────────────────────────────────────────────────
        self.declare_parameter('free_threshold',    self._FREE_THRESH_DEF)
        self.declare_parameter('min_frontier_size', self._MIN_FRONTIER_DEF)
        self.declare_parameter('goal_tolerance',    self._GOAL_TOL_DEF)
        self.declare_parameter('nav_timeout',       self._NAV_TIMEOUT_DEF)
        self.declare_parameter('update_rate',       self._UPDATE_RATE_DEF)
        self.declare_parameter('blacklist_radius',  self._BL_RADIUS_DEF)

        self._free_thresh  = self.get_parameter('free_threshold').value
        self._min_frontier = self.get_parameter('min_frontier_size').value
        self._goal_tol     = self.get_parameter('goal_tolerance').value
        self._nav_timeout  = self.get_parameter('nav_timeout').value
        self._bl_radius    = self.get_parameter('blacklist_radius').value

        # ── Internal state ────────────────────────────────────────────────────
        self._map: OccupancyGrid | None = None
        self._map_lock    = threading.Lock()
        self._navigating  = False
        self._goal_handle = None
        self._cur_goal    = None        # (x, y) world coords
        self._nav_start   = None
        self._blacklist   = []          # [(x, y)] — goals that previously failed
        self._done        = False
        self._robot_x     = 0.0
        self._robot_y     = 0.0

        # ── TF ────────────────────────────────────────────────────────────────
        self._tf_buffer   = tf2_ros.Buffer()
        self._tf_listener = tf2_ros.TransformListener(self._tf_buffer, self)

        # ── ROS interfaces ────────────────────────────────────────────────────
        self.create_subscription(OccupancyGrid, '/map', self._map_cb, 10)
        self._marker_pub  = self.create_publisher(MarkerArray, '/frontier_markers', 10)
        self._nav_client  = ActionClient(self, NavigateToPose, 'navigate_to_pose')

        rate = self.get_parameter('update_rate').value
        self.create_timer(1.0 / rate, self._tick)

        self.get_logger().info(
            'FrontierExplorer started — '
            f'min_frontier={self._min_frontier} cells, '
            f'nav_timeout={self._nav_timeout}s, '
            f'update_rate={rate}Hz'
        )
        self.get_logger().info('Waiting for /map …')

    # ── Map callback ──────────────────────────────────────────────────────────

    def _map_cb(self, msg: OccupancyGrid):
        with self._map_lock:
            self._map = msg

    # ── Main exploration tick (timer callback) ────────────────────────────────

    def _tick(self):
        if self._done:
            return

        # Grab a snapshot of the current map
        with self._map_lock:
            occ_map = self._map
        if occ_map is None:
            return

        # Get current robot pose
        rx, ry, ok = self._robot_pose()
        if not ok:
            return
        self._robot_x, self._robot_y = rx, ry

        # If a goal is active, just check for timeout
        if self._navigating:
            elapsed = (self.get_clock().now() - self._nav_start).nanoseconds / 1e9
            if elapsed > self._nav_timeout:
                self.get_logger().warn(
                    f'Goal ({self._cur_goal[0]:.2f}, {self._cur_goal[1]:.2f}) '
                    f'timed out after {elapsed:.0f}s — blacklisting'
                )
                if self._cur_goal:
                    self._blacklist.append(self._cur_goal)
                self._cancel_goal()
            return

        # Detect and cluster frontiers
        clusters = self._detect_frontier_clusters(occ_map)
        self._publish_markers(clusters, occ_map)

        if not clusters:
            self.get_logger().info(
                '╔══════════════════════════════════════════════════════════╗\n'
                '║  EXPLORATION COMPLETE — no unexplored frontiers remain!  ║\n'
                '║  Save your map:                                          ║\n'
                '║    ros2 service call /slam_toolbox/serialize_map \\       ║\n'
                '║      slam_toolbox/srv/SerializePoseGraph \\               ║\n'
                '║      "{filename: \'~/maps/indoor_map\'}"                   ║\n'
                '╚══════════════════════════════════════════════════════════╝'
            )
            self._done = True
            return

        goal = self._best_frontier(clusters, rx, ry)
        if goal is None:
            self.get_logger().warn(
                f'All {len(clusters)} frontiers are blacklisted — clearing blacklist'
            )
            self._blacklist.clear()
            return

        gx, gy = goal
        self.get_logger().info(
            f'→ Sending goal ({gx:.2f}, {gy:.2f}) | '
            f'{len(clusters)} frontier clusters | '
            f'dist={math.hypot(gx - rx, gy - ry):.2f}m'
        )
        self._send_goal(gx, gy)

    # ── Robot pose via TF ─────────────────────────────────────────────────────

    def _robot_pose(self):
        """Returns (x, y, success) in the map frame."""
        try:
            tf = self._tf_buffer.lookup_transform(
                'map', 'base_link',
                rclpy.time.Time(),
                timeout=Duration(seconds=0.3),
            )
            return (
                tf.transform.translation.x,
                tf.transform.translation.y,
                True,
            )
        except TransformException:
            return 0.0, 0.0, False

    # ── Frontier detection ────────────────────────────────────────────────────

    def _detect_frontier_clusters(self, occ_map: OccupancyGrid):
        """
        Returns a list of frontier clusters:
            [(world_x, world_y, size_in_cells), …]

        A frontier cell is a FREE cell (occupancy 0–free_threshold) that has
        at least one UNKNOWN (occupancy −1) cell among its 4-connected neighbors.
        Adjacent frontier cells are clustered via BFS.
        Clusters smaller than min_frontier_size are discarded.
        """
        w   = occ_map.info.width
        h   = occ_map.info.height
        res = occ_map.info.resolution
        ox  = occ_map.info.origin.position.x
        oy  = occ_map.info.origin.position.y

        grid = np.array(occ_map.data, dtype=np.int8).reshape(h, w)

        # Boolean masks
        free    = (grid >= 0) & (grid <= self._free_thresh)
        unknown = (grid == -1)

        # Frontier mask: free cells with at least one unknown 4-neighbor (vectorised)
        frontier = np.zeros((h, w), dtype=bool)
        frontier[1:-1, 1:-1] = free[1:-1, 1:-1] & (
            unknown[:-2, 1:-1]   # above
            | unknown[2:,  1:-1] # below
            | unknown[1:-1, :-2] # left
            | unknown[1:-1, 2:]  # right
        )

        # BFS clustering over frontier cells
        visited  = np.zeros((h, w), dtype=bool)
        clusters = []
        f_rows, f_cols = np.where(frontier)

        for r0, c0 in zip(f_rows.tolist(), f_cols.tolist()):
            r0, c0 = int(r0), int(c0)
            if visited[r0, c0]:
                continue

            # BFS — collect all connected frontier cells
            cluster = []
            queue   = collections.deque([(r0, c0)])
            visited[r0, c0] = True

            while queue:
                r, c = queue.popleft()
                cluster.append((r, c))
                for dr, dc in ((-1, 0), (1, 0), (0, -1), (0, 1)):
                    nr, nc = r + dr, c + dc
                    if (0 <= nr < h and 0 <= nc < w
                            and frontier[nr, nc]
                            and not visited[nr, nc]):
                        visited[nr, nc] = True
                        queue.append((nr, nc))

            if len(cluster) < self._min_frontier:
                continue

            # Centroid in world coordinates
            cr = sum(p[0] for p in cluster) / len(cluster)
            cc = sum(p[1] for p in cluster) / len(cluster)
            wx = ox + (cc + 0.5) * res
            wy = oy + (cr + 0.5) * res
            clusters.append((wx, wy, len(cluster)))

        return clusters

    # ── Frontier selection ────────────────────────────────────────────────────

    def _best_frontier(self, clusters, rx, ry):
        """
        Score = cluster_size / sqrt(distance + 0.1)

        Balances information gain (larger frontier → more new area) against
        travel cost (closer → less time wasted).  Blacklisted locations are
        skipped.

        Returns (x, y) of the best frontier centroid, or None if all are
        blacklisted.
        """
        best_score = -1.0
        best       = None

        for wx, wy, size in clusters:
            # Skip blacklisted goals
            if any(math.hypot(wx - bx, wy - by) < self._bl_radius
                   for bx, by in self._blacklist):
                continue
            dist  = math.hypot(wx - rx, wy - ry) + 0.1
            score = size / math.sqrt(dist)
            if score > best_score:
                best_score = score
                best       = (wx, wy)

        return best

    # ── Nav2 goal dispatch ────────────────────────────────────────────────────

    def _send_goal(self, x: float, y: float):
        if not self._nav_client.wait_for_server(timeout_sec=2.0):
            self.get_logger().warn(
                'NavigateToPose action server not available — '
                'is bt_navigator running?'
            )
            return

        goal_msg = NavigateToPose.Goal()
        goal_msg.pose.header.frame_id = 'map'
        goal_msg.pose.header.stamp    = self.get_clock().now().to_msg()
        goal_msg.pose.pose.position.x = x
        goal_msg.pose.pose.position.y = y
        goal_msg.pose.pose.orientation.w = 1.0   # heading unconstrained

        self._cur_goal   = (x, y)
        self._navigating = True
        self._nav_start  = self.get_clock().now()

        future = self._nav_client.send_goal_async(
            goal_msg,
            feedback_callback=self._nav_feedback_cb,
        )
        future.add_done_callback(self._goal_response_cb)

    def _goal_response_cb(self, future):
        handle = future.result()
        if not handle.accepted:
            self.get_logger().warn('Goal rejected by Nav2 — skipping')
            self._navigating = False
            self._cur_goal   = None
            return
        self._goal_handle = handle
        handle.get_result_async().add_done_callback(self._result_cb)

    def _result_cb(self, future):
        status = future.result().status
        if status == GoalStatus.STATUS_SUCCEEDED:
            self.get_logger().info('Frontier reached ✓')
        else:
            self.get_logger().warn(
                f'Navigation failed (status={status}) — blacklisting '
                f'({self._cur_goal[0]:.2f}, {self._cur_goal[1]:.2f})'
            )
            if self._cur_goal:
                self._blacklist.append(self._cur_goal)

        self._navigating  = False
        self._cur_goal    = None
        self._goal_handle = None

    def _nav_feedback_cb(self, feedback_msg):
        # Optionally log distance remaining
        # dist = feedback_msg.feedback.distance_remaining
        pass

    def _cancel_goal(self):
        self._navigating  = False
        self._cur_goal    = None
        if self._goal_handle is not None:
            self._goal_handle.cancel_goal_async()
            self._goal_handle = None

    # ── RViz visualisation ────────────────────────────────────────────────────

    def _publish_markers(self, clusters, occ_map: OccupancyGrid):
        """
        Publish cylinder markers for each frontier cluster (green) and an
        arrow marker for the current navigation goal (orange).
        """
        ma  = MarkerArray()
        now = self.get_clock().now().to_msg()
        res = occ_map.info.resolution

        # Delete all previous markers first
        clear = Marker()
        clear.action = Marker.DELETEALL
        ma.markers.append(clear)

        # Frontier cluster markers
        for i, (wx, wy, size) in enumerate(clusters):
            m = Marker()
            m.header.frame_id = 'map'
            m.header.stamp    = now
            m.ns              = 'frontiers'
            m.id              = i + 1
            m.type            = Marker.CYLINDER
            m.action          = Marker.ADD
            m.pose.position.x = wx
            m.pose.position.y = wy
            m.pose.position.z = 0.05
            m.pose.orientation.w = 1.0
            sc          = max(0.12, min(0.55, size * res))
            m.scale.x   = sc
            m.scale.y   = sc
            m.scale.z   = 0.15
            m.color.r   = 0.0
            m.color.g   = 0.9
            m.color.b   = 0.4
            m.color.a   = 0.85
            ma.markers.append(m)

        # Current goal marker (orange arrow)
        if self._cur_goal:
            gx, gy = self._cur_goal
            arrow = Marker()
            arrow.header.frame_id = 'map'
            arrow.header.stamp    = now
            arrow.ns              = 'frontier_goal'
            arrow.id              = 0
            arrow.type            = Marker.ARROW
            arrow.action          = Marker.ADD
            arrow.pose.position.x = gx
            arrow.pose.position.y = gy
            arrow.pose.position.z = 0.35
            arrow.pose.orientation.x = 0.0
            arrow.pose.orientation.y = 0.707
            arrow.pose.orientation.z = 0.0
            arrow.pose.orientation.w = 0.707   # point downward
            arrow.scale.x = 0.40
            arrow.scale.y = 0.10
            arrow.scale.z = 0.10
            arrow.color.r = 1.0
            arrow.color.g = 0.5
            arrow.color.b = 0.0
            arrow.color.a = 1.0
            ma.markers.append(arrow)

        # Robot position marker (blue dot)
        robot_m = Marker()
        robot_m.header.frame_id = 'map'
        robot_m.header.stamp    = now
        robot_m.ns              = 'explorer_robot'
        robot_m.id              = 0
        robot_m.type            = Marker.SPHERE
        robot_m.action          = Marker.ADD
        robot_m.pose.position.x = self._robot_x
        robot_m.pose.position.y = self._robot_y
        robot_m.pose.position.z = 0.10
        robot_m.pose.orientation.w = 1.0
        robot_m.scale.x = 0.20
        robot_m.scale.y = 0.20
        robot_m.scale.z = 0.20
        robot_m.color.r = 0.2
        robot_m.color.g = 0.5
        robot_m.color.b = 1.0
        robot_m.color.a = 0.9
        ma.markers.append(robot_m)

        self._marker_pub.publish(ma)


def main(args=None):
    rclpy.init(args=args)
    node = FrontierExplorer()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == '__main__':
    main()
