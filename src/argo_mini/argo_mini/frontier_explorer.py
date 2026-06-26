#!/usr/bin/env python3
"""
frontier_explorer.py – Autonomous frontier exploration for Argo Mini.

Subscribes to /map (OccupancyGrid from SLAM Toolbox in async-mapping mode),
detects frontier cells (free cells adjacent to unknown cells), clusters them
with BFS, scores each cluster by size / sqrt(distance), and sends the best
frontier centroid as a NavigateToPose action goal.

Blacklists failed goal positions and backs off after repeated failures.
Publishes /frontier_markers (green cylinders in RViz, yellow = selected).
"""

import math
import time
from collections import deque

import numpy as np
import rclpy
import rclpy.duration
import rclpy.time
from action_msgs.msg import GoalStatus
from geometry_msgs.msg import PoseStamped
from nav2_msgs.action import NavigateToPose
from nav_msgs.msg import OccupancyGrid
from rclpy.action import ActionClient
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, HistoryPolicy, QoSProfile, ReliabilityPolicy
from tf2_ros import Buffer, TransformListener
from visualization_msgs.msg import Marker, MarkerArray

# ── Frontier detection ────────────────────────────────────────────────────────
FREE_THRESH    = 25      # occupancy 0..FREE_THRESH counts as free
MIN_CLUSTER_SZ = 5       # discard clusters smaller than this (noise)

# ── Goal management ───────────────────────────────────────────────────────────
GOAL_TIMEOUT   = 45.0    # s – cancel stalled goals
BACKOFF_CONSEC = 3       # consecutive failures before pausing
BACKOFF_SECS   = 8.0     # s – pause length after BACKOFF_CONSEC failures
BLACKLIST_DIST = 0.30    # m – radius around a failed goal to blacklist

# ── Timer ─────────────────────────────────────────────────────────────────────
TICK_SECS      = 2.0     # s – how often to look for a new frontier


class FrontierExplorer(Node):

    def __init__(self):
        super().__init__("frontier_explorer")

        # ── Shared state ──────────────────────────────────────────────────────
        self._map: OccupancyGrid | None = None

        self._goal_handle   = None
        self._goal_active   = False
        self._goal_sent_at  = 0.0
        self._last_goal_wx  = 0.0
        self._last_goal_wy  = 0.0
        self._cancelling    = False   # True while waiting for cancel to land

        self._consec_fails  = 0
        self._backoff_until = 0.0
        self._blacklist: list[tuple[float, float]] = []

        # ── TF ────────────────────────────────────────────────────────────────
        self._tf_buffer   = Buffer()
        self._tf_listener = TransformListener(self._tf_buffer, self)

        # ── /map subscription (transient-local – receive last published map) ──
        map_qos = QoSProfile(
            reliability=ReliabilityPolicy.RELIABLE,
            history=HistoryPolicy.KEEP_LAST,
            depth=1,
            durability=DurabilityPolicy.TRANSIENT_LOCAL,
        )
        self.create_subscription(OccupancyGrid, "/map", self._on_map, map_qos)

        # ── Action client + marker publisher ──────────────────────────────────
        self._action_client = ActionClient(self, NavigateToPose, "navigate_to_pose")
        self._marker_pub = self.create_publisher(MarkerArray, "/frontier_markers", 10)

        # ── Main exploration timer ─────────────────────────────────────────────
        self.create_timer(TICK_SECS, self._tick)

        self.get_logger().info("[FrontierExplorer] ready – waiting for /map and TF")

    # ── Map callback ──────────────────────────────────────────────────────────

    def _on_map(self, msg: OccupancyGrid) -> None:
        self._map = msg

    # ── Robot pose ────────────────────────────────────────────────────────────

    def _robot_xy(self) -> tuple[float, float] | None:
        try:
            t = self._tf_buffer.lookup_transform(
                "map", "base_link",
                rclpy.time.Time(),
                timeout=rclpy.duration.Duration(seconds=0.5),
            )
            return t.transform.translation.x, t.transform.translation.y
        except Exception:
            return None

    # ── Main exploration tick ─────────────────────────────────────────────────

    def _tick(self) -> None:
        if self._map is None:
            return

        # If a goal is active, only check for timeout.
        if self._goal_active:
            if time.monotonic() - self._goal_sent_at > GOAL_TIMEOUT:
                self.get_logger().warn("[FrontierExplorer] goal timed out – cancelling")
                self._cancel_current()
                self._on_fail()
            return

        if time.monotonic() < self._backoff_until:
            return

        pose = self._robot_xy()
        if pose is None:
            self.get_logger().warn("[FrontierExplorer] no TF map→base_link yet")
            return

        rx, ry   = pose
        clusters = self._detect_frontiers(rx, ry)

        if not clusters:
            self.get_logger().info(
                "[FrontierExplorer] no frontiers found – map may be fully explored")
            return

        valid = [c for c in clusters if not self._blacklisted(c["wx"], c["wy"])]
        if not valid:
            self.get_logger().warn(
                "[FrontierExplorer] all frontiers blacklisted – clearing blacklist")
            self._blacklist.clear()
            valid = clusters

        best = max(valid, key=lambda c: c["score"])
        self._publish_markers(clusters, best)
        self._send_goal(best["wx"], best["wy"])

    # ── Frontier detection & clustering ──────────────────────────────────────

    def _detect_frontiers(self, rx: float, ry: float) -> list[dict]:
        m   = self._map
        w   = m.info.width
        h   = m.info.height
        res = m.info.resolution
        ox  = m.info.origin.position.x
        oy  = m.info.origin.position.y

        # OccupancyGrid.data is int8: -1 unknown, 0-100 probability occupied.
        data = np.array(m.data, dtype=np.int8).reshape(h, w)

        free    = (data >= 0) & (data <= FREE_THRESH)
        unknown = data == -1

        # Frontier cell = free cell adjacent (4-connected) to an unknown cell.
        adj_unknown = (
            np.roll(unknown,  1, axis=0) |
            np.roll(unknown, -1, axis=0) |
            np.roll(unknown,  1, axis=1) |
            np.roll(unknown, -1, axis=1)
        )
        # np.roll wraps edges – zero them out to avoid phantom frontiers.
        adj_unknown[0,  :] = False
        adj_unknown[-1, :] = False
        adj_unknown[:,  0] = False
        adj_unknown[:, -1] = False

        frontier_mask = free & adj_unknown
        front_ys, front_xs = np.where(frontier_mask)
        if len(front_ys) == 0:
            return []

        # BFS clustering – group spatially connected frontier cells.
        visited  = np.zeros((h, w), dtype=bool)
        front_set: set[tuple[int, int]] = set(
            zip(front_ys.tolist(), front_xs.tolist())
        )
        clusters: list[dict] = []

        for r0, c0 in front_set:
            if visited[r0, c0]:
                continue

            q       = deque([(r0, c0)])
            visited[r0, c0] = True
            members: list[tuple[int, int]] = [(r0, c0)]

            while q:
                r, c = q.popleft()
                for dr, dc in ((-1, 0), (1, 0), (0, -1), (0, 1)):
                    nr, nc = r + dr, c + dc
                    if (nr, nc) in front_set and not visited[nr, nc]:
                        visited[nr, nc] = True
                        q.append((nr, nc))
                        members.append((nr, nc))

            if len(members) < MIN_CLUSTER_SZ:
                continue

            rows = np.array([r for r, _ in members], dtype=float)
            cols = np.array([c for _, c in members], dtype=float)

            # Centroid in world coordinates (cell centre = idx + 0.5).
            wx = ox + (cols.mean() + 0.5) * res
            wy = oy + (rows.mean() + 0.5) * res

            dist  = max(math.hypot(wx - rx, wy - ry), 0.01)
            score = len(members) / math.sqrt(dist)

            clusters.append({
                "wx":    wx,
                "wy":    wy,
                "size":  len(members),
                "dist":  dist,
                "score": score,
            })

        return clusters

    # ── Blacklist helpers ─────────────────────────────────────────────────────

    def _blacklisted(self, wx: float, wy: float) -> bool:
        return any(
            math.hypot(wx - bx, wy - by) < BLACKLIST_DIST
            for bx, by in self._blacklist
        )

    # ── Action goal ───────────────────────────────────────────────────────────

    def _send_goal(self, wx: float, wy: float) -> None:
        if not self._action_client.wait_for_server(timeout_sec=2.0):
            self.get_logger().warn("[FrontierExplorer] NavigateToPose server not ready")
            return

        self._last_goal_wx = wx
        self._last_goal_wy = wy

        goal = NavigateToPose.Goal()
        goal.pose.header.frame_id      = "map"
        goal.pose.header.stamp         = self.get_clock().now().to_msg()
        goal.pose.pose.position.x      = wx
        goal.pose.pose.position.y      = wy
        goal.pose.pose.orientation.w   = 1.0
        goal.behavior_tree             = ""

        self._goal_active  = True
        self._goal_sent_at = time.monotonic()
        self._cancelling   = False

        self.get_logger().info(
            f"[FrontierExplorer] → frontier ({wx:.2f}, {wy:.2f})")

        fut = self._action_client.send_goal_async(goal)
        fut.add_done_callback(self._on_goal_response)

    def _on_goal_response(self, future) -> None:
        handle = future.result()
        if handle is None or not handle.accepted:
            self.get_logger().warn("[FrontierExplorer] goal rejected by Nav2")
            self._goal_active = False
            self._on_fail()
            return
        self._goal_handle = handle
        handle.get_result_async().add_done_callback(self._on_goal_result)

    def _on_goal_result(self, future) -> None:
        self._goal_active = False
        self._goal_handle = None

        # Ignore results that arrive after we triggered a manual cancel.
        if self._cancelling:
            self._cancelling = False
            return

        result = future.result()
        if result is not None and result.status == GoalStatus.STATUS_SUCCEEDED:
            self.get_logger().info("[FrontierExplorer] goal succeeded")
            self._consec_fails = 0
        else:
            status = result.status if result else "none"
            self.get_logger().warn(
                f"[FrontierExplorer] goal failed (status={status})")
            self._on_fail()

    def _cancel_current(self) -> None:
        if self._goal_handle is not None:
            self._cancelling = True
            self._goal_handle.cancel_goal_async()
        self._goal_active = False
        self._goal_handle = None

    def _on_fail(self) -> None:
        self._blacklist.append((self._last_goal_wx, self._last_goal_wy))
        self._consec_fails += 1
        if self._consec_fails >= BACKOFF_CONSEC:
            self.get_logger().warn(
                f"[FrontierExplorer] {self._consec_fails} consecutive failures – "
                f"backing off {BACKOFF_SECS:.0f}s"
            )
            self._backoff_until = time.monotonic() + BACKOFF_SECS
            self._consec_fails  = 0

    # ── RViz markers ──────────────────────────────────────────────────────────

    def _publish_markers(self, clusters: list[dict], best: dict) -> None:
        arr = MarkerArray()
        now = self.get_clock().now().to_msg()

        clear = Marker()
        clear.action = Marker.DELETEALL
        arr.markers.append(clear)

        for i, c in enumerate(clusters):
            mk                    = Marker()
            mk.header.frame_id    = "map"
            mk.header.stamp       = now
            mk.ns                 = "frontiers"
            mk.id                 = i + 1
            mk.type               = Marker.CYLINDER
            mk.action             = Marker.ADD
            mk.pose.position.x    = c["wx"]
            mk.pose.position.y    = c["wy"]
            mk.pose.position.z    = 0.25
            mk.pose.orientation.w = 1.0
            mk.scale.x            = 0.15
            mk.scale.y            = 0.15
            mk.scale.z            = 0.50
            mk.lifetime.sec       = 5
            mk.color.a            = 1.0
            if c is best:
                mk.color.r = 1.0; mk.color.g = 1.0; mk.color.b = 0.0  # yellow
            else:
                mk.color.r = 0.0; mk.color.g = 1.0; mk.color.b = 0.0  # green
            arr.markers.append(mk)

        self._marker_pub.publish(arr)


def main(args=None):
    rclpy.init(args=args)
    node = FrontierExplorer()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
