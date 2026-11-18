"""ROS 2 (Humble) backend: Nav2 goals, 1 m moves / in-place turns, and obstacle clearance.

All velocity commands go to /cmd_vel_smoothed, i.e. *through* the depth/lidar/ultrasonic safety shield
(see src/argo_mini/argo_mini/safety_shield.py) – we never bypass it. On top of that we pre-check
clearance in the requested direction so the robot can say "I'm blocked" instead of silently refusing,
and we detect a stall mid-move (shield zeroed our command) and report it as blocked too.

One persistent rclpy node spun in a background thread (no per-command subprocess like nav_bridge.py,
which costs 1-2 s of rclpy start-up per trip).
"""
from __future__ import annotations

import asyncio
import logging
import math
import threading
import time
from typing import Optional

import numpy as np

from ..config import Settings
from .base import MoveResult, Robot

log = logging.getLogger("nlp.ros")
STALE_S = 1.0
# Direction -> heading offset (rad, CCW, 0 = robot front) used for clearance checks.
_ANGLE = {"forward": 0.0, "left": math.pi / 2, "backward": math.pi, "right": -math.pi / 2}


def _wrap(a: float) -> float:
    return (a + math.pi) % (2 * math.pi) - math.pi


class RosRobot(Robot):
    def __init__(self, s: Settings):
        self.s = s
        self._pts: Optional[np.ndarray] = None       # (N,2) lidar points in robot frame
        self._pts_t = 0.0
        self._us: dict[str, tuple[float, float]] = {}  # name -> (range, stamp)
        self._depth: tuple[float, float] = (math.inf, 0.0)
        self._pose: Optional[tuple[float, float, float]] = None
        self._lock = threading.Lock()

    # ---------------------------------------------------------------- lifecycle
    async def start(self) -> None:
        import rclpy
        from geometry_msgs.msg import Twist
        from nav2_msgs.action import NavigateToPose
        from nav_msgs.msg import Odometry
        from rclpy.action import ActionClient
        from rclpy.executors import MultiThreadedExecutor
        from rclpy.node import Node
        from rclpy.qos import QoSProfile, ReliabilityPolicy
        from sensor_msgs.msg import LaserScan, Range
        from std_msgs.msg import Float32

        self._Twist, self._Nav = Twist, NavigateToPose
        if not rclpy.ok():
            rclpy.init()
        self.node = Node("sonic_nlp")
        be = QoSProfile(reliability=ReliabilityPolicy.BEST_EFFORT, depth=5)
        self.node.create_subscription(LaserScan, "/scan_corrected", self._on_scan, be)
        self.node.create_subscription(Odometry, "/odom", self._on_odom, 10)
        self.node.create_subscription(Float32, "/safety_shield/depth_distance", self._on_depth, be)
        for n in ("front_left", "front_right"):
            self.node.create_subscription(Range, f"/us/{n}", lambda m, n=n: self._on_us(n, m), be)
        self.pub = self.node.create_publisher(Twist, self.s.cmd_topic, 10)
        self.nav = ActionClient(self.node, NavigateToPose, "/navigate_to_pose")
        self._exec = MultiThreadedExecutor(num_threads=2)
        self._exec.add_node(self.node)
        threading.Thread(target=self._exec.spin, daemon=True, name="rclpy-spin").start()
        log.info("ROS node up (cmd topic %s)", self.s.cmd_topic)

    async def close(self) -> None:
        import rclpy
        self._stop()
        self._exec.shutdown()
        self.node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()

    # ---------------------------------------------------------------- sensor callbacks (executor thread)
    def _on_scan(self, m) -> None:
        r = np.asarray(m.ranges, dtype=np.float32)
        a = m.angle_min + np.arange(len(r), dtype=np.float32) * m.angle_increment + self.s.lidar_yaw_offset_rad
        ok = np.isfinite(r) & (r > max(m.range_min, 0.05)) & (r < m.range_max)
        r, a = r[ok], a[ok]
        pts = np.stack([r * np.cos(a), r * np.sin(a)], axis=1)
        with self._lock:
            self._pts, self._pts_t = pts, time.monotonic()

    def _on_odom(self, m) -> None:
        q = m.pose.pose.orientation
        yaw = math.atan2(2 * (q.w * q.z + q.x * q.y), 1 - 2 * (q.y * q.y + q.z * q.z))
        with self._lock:
            self._pose = (m.pose.pose.position.x, m.pose.pose.position.y, yaw)

    def _on_depth(self, m) -> None:
        with self._lock:
            self._depth = (float(m.data) if m.data > 0 else math.inf, time.monotonic())

    def _on_us(self, name: str, m) -> None:
        v = float(m.range)
        valid = m.min_range < v < m.max_range
        with self._lock:
            self._us[name] = (v if valid else math.inf, time.monotonic())

    # ---------------------------------------------------------------- clearance
    def _lidar_clear(self, theta: float) -> Optional[float]:
        with self._lock:
            pts, t = self._pts, self._pts_t
        if pts is None or time.monotonic() - t > STALE_S:
            return None
        c, s = math.cos(theta), math.sin(theta)
        x = pts[:, 0] * c + pts[:, 1] * s
        y = -pts[:, 0] * s + pts[:, 1] * c
        m = (x > 0) & (np.abs(y) < self.s.corridor_half_width_m)
        return float(x[m].min()) if m.any() else math.inf

    def _fresh(self, key: str) -> Optional[float]:
        with self._lock:
            v = self._us.get(key)
        return v[0] if v and time.monotonic() - v[1] <= STALE_S else None

    def clearance(self, direction: str) -> Optional[float]:
        d = {"turn_left": "left", "turn_right": "right"}.get(direction, direction)
        if d == "spin":
            return self._ring_clearance()
        vals = [self._lidar_clear(_ANGLE[d])]
        if d == "forward":
            with self._lock:
                dep = self._depth
            vals.append(dep[0] if time.monotonic() - dep[1] <= STALE_S else None)
            vals += [self._fresh("front_left"), self._fresh("front_right")]
        vals = [v for v in vals if v is not None]
        return min(vals) if vals else None

    def _ring_clearance(self) -> Optional[float]:
        """Nearest obstacle in any direction (for spinning) = min of lidar ring + all ultrasonics."""
        with self._lock:
            pts, t = self._pts, self._pts_t
        vals = []
        if pts is not None and time.monotonic() - t <= STALE_S:
            vals.append(float(np.hypot(pts[:, 0], pts[:, 1]).min()) if len(pts) else math.inf)
        vals += [v for v in (self._fresh(k) for k in ("front_left", "front_right")) if v is not None]
        return min(vals) if vals else None

    # ---------------------------------------------------------------- motion
    def _twist(self, lin: float = 0.0, ang: float = 0.0) -> None:
        m = self._Twist()
        m.linear.x, m.angular.z = float(lin), float(ang)
        self.pub.publish(m)

    def _stop(self) -> None:
        for _ in range(3):
            self._twist(0.0, 0.0)

    def _get_pose(self) -> Optional[tuple[float, float, float]]:
        with self._lock:
            return self._pose

    async def move(self, direction: str) -> MoveResult:
        s = self.s
        if self._get_pose() is None:
            return MoveResult("no_sensors", detail="no odometry")
        clear = self.clearance(direction)
        if clear is None and s.require_sensors:
            return MoveResult("no_sensors")
        try:
            if direction in ("turn_left", "turn_right", "spin"):
                need = s.robot_radius_m + 0.15
                if clear is not None and clear < need:
                    return MoveResult("blocked", detail=f"{clear:.2f} m")
                ang = {"turn_left": math.pi / 2, "turn_right": -math.pi / 2, "spin": 2 * math.pi}[direction]
                return await self._rotate(ang)
            need = s.move_distance_m + s.clearance_margin_m
            if clear is not None and clear < need:
                return MoveResult("blocked", detail=f"{clear:.2f} m")
            if direction in ("left", "right"):
                r = await self._rotate(math.pi / 2 if direction == "left" else -math.pi / 2)
                if not r.ok:
                    return r
                # after turning, "ahead" is the side we checked
                return await self._translate(+1)
            return await self._translate(+1 if direction == "forward" else -1)
        except asyncio.CancelledError:
            raise
        finally:
            self._stop()

    async def _translate(self, sign: int) -> MoveResult:
        s = self.s
        x0, y0, _ = self._get_pose()
        t0 = t_prog = time.monotonic()
        last = 0.0
        direction = "forward" if sign > 0 else "backward"
        while True:
            x, y, _ = self._get_pose()
            d = math.hypot(x - x0, y - y0)
            if d >= s.move_distance_m - 0.02:
                return MoveResult("ok", d)
            now = time.monotonic()
            if d - last > 0.01:
                last, t_prog = d, now
            if now - t_prog > 1.5 or now - t0 > s.move_distance_m / s.move_speed * 3 + 3:
                return MoveResult("blocked", d, "stalled (safety shield)")
            c = self.clearance(direction)
            if c is not None and c < s.stop_dist_m:
                return MoveResult("blocked", d, f"{c:.2f} m")
            self._twist(sign * s.move_speed, 0.0)
            await asyncio.sleep(0.05)

    async def _rotate(self, delta: float) -> MoveResult:
        s = self.s
        _, _, prev = self._get_pose()
        turned, t0, t_prog, last = 0.0, time.monotonic(), time.monotonic(), 0.0
        sign = 1 if delta > 0 else -1
        while True:
            _, _, yaw = self._get_pose()
            turned += _wrap(yaw - prev)
            prev = yaw
            err = delta - turned
            if abs(err) < 0.04:
                return MoveResult("ok")
            now = time.monotonic()
            if abs(turned) - last > 0.01:
                last, t_prog = abs(turned), now
            if now - t_prog > 1.5 or now - t0 > abs(delta) / s.turn_speed * 3 + 3:
                return MoveResult("blocked", detail="rotation stalled")
            w = max(0.2, min(s.turn_speed, abs(err) * 1.5))
            self._twist(0.0, math.copysign(w, err if abs(err) > 0 else sign))
            await asyncio.sleep(0.05)

    # ---------------------------------------------------------------- navigation
    def _aw(self, rfut) -> asyncio.Future:
        loop = asyncio.get_running_loop()
        af = loop.create_future()

        def done(f):
            loop.call_soon_threadsafe(lambda: af.done() or af.set_result(f.result()))
        rfut.add_done_callback(done)
        return af

    async def navigate(self, waypoint: dict) -> bool:
        if not self.nav.wait_for_server(timeout_sec=0.0) and not await asyncio.to_thread(self.nav.wait_for_server, 3.0):
            log.error("/navigate_to_pose not available – is Nav2 running?")
            return False
        goal = self._Nav.Goal()
        goal.pose.header.frame_id = "map"
        goal.pose.header.stamp = self.node.get_clock().now().to_msg()
        goal.pose.pose.position.x = float(waypoint["x"])
        goal.pose.pose.position.y = float(waypoint["y"])
        goal.pose.pose.orientation.z = float(waypoint.get("qz", 0.0))
        goal.pose.pose.orientation.w = float(waypoint.get("qw", 1.0))
        handle = await self._aw(self.nav.send_goal_async(goal))
        if not handle.accepted:
            return False
        try:
            res = await asyncio.wait_for(self._aw(handle.get_result_async()), self.s.nav_timeout_s)
            return res.status == 4        # GoalStatus.STATUS_SUCCEEDED
        except (asyncio.CancelledError, asyncio.TimeoutError):
            handle.cancel_goal_async()
            raise
