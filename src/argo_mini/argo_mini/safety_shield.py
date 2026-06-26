#!/usr/bin/env python3
"""
safety_shield.py – Unified velocity safety gate for Argo Mini.

Pipeline:
    /cmd_vel_smoothed  –>  [safety_shield]  –>  /cmd_vel

Three independent sensor sources with graduated forward-speed response:

  Lidar (LaserScan)  – /scan_corrected
    > LIDAR_SLOW_DIST (1.00 m) : full speed
    LIDAR_STOP_DIST .. LIDAR_SLOW_DIST : speed scaled linearly 0→100%
    < LIDAR_STOP_DIST (0.45 m) : hard stop

  Depth camera (PointCloud2)  – raw /points (independent of restamper)
    > DEPTH_SLOW_DIST (0.80 m) : full speed
    DEPTH_STOP_DIST .. DEPTH_SLOW_DIST : speed scaled linearly 0→100%
    < DEPTH_STOP_DIST (0.45 m) : hard stop

  Ultrasonic (Range x 4)  – binary, no slow zone needed at 10 cm
    FL/FR < US_FRONT_DIST (0.10 m) : hard stop forward
    BL/BR < US_REAR_DIST  (0.10 m) : hard stop reverse

Gate:
    fwd_scale = min(lidar_scale, depth_scale, us_scale)   ∈ [0.0, 1.0]
    linear.x  = commanded_linear.x * fwd_scale            (forward only)
    reverse   = blocked by US rear (binary)
    angular.z = always passes through

All sensors stale → full pass-through (fail-open, robot never freezes).
"""

import math
import threading
import time

import numpy as np
import rclpy
from geometry_msgs.msg import Twist
from rclpy.callback_groups import MutuallyExclusiveCallbackGroup
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node
from rclpy.qos import HistoryPolicy, QoSProfile, ReliabilityPolicy
from sensor_msgs.msg import LaserScan, PointCloud2, Range

# ── Lidar ─────────────────────────────────────────────────────────────────────
LIDAR_SLOW_DIST  = 1.00    # m   – begin speed reduction
LIDAR_STOP_DIST  = 0.45    # m   – hard stop
LIDAR_WIDTH_HALF = 0.35    # m   – half-width of danger corridor
LIDAR_MIN_PTS    = 3       # minimum scan points to register an obstacle
LIDAR_STALE_SECS = 1.0     # s   – treat as stale if no scan arrives

# ── Depth camera ──────────────────────────────────────────────────────────────
DEPTH_SLOW_DIST  = 0.80    # m   – begin speed reduction
DEPTH_STOP_DIST  = 0.45    # m   – hard stop
DEPTH_SLOW_PTS   = 5       # minimum pts to activate slow zone
DEPTH_MIN_PTS    = 15      # minimum pts for hard stop (noise filter)
DEPTH_WIDTH_HALF = 0.40    # m   – half-width of danger corridor
DEPTH_HEIGHT_MIN = -1.30   # opt Y – lower bound (ignore floor)
DEPTH_HEIGHT_MAX =  0.05   # opt Y – upper bound (ignore ceiling)
DEPTH_STALE_SECS = 1.0     # s

# ── Ultrasonic ────────────────────────────────────────────────────────────────
US_FRONT_DIST    = 0.10    # m   – hard stop forward
US_REAR_DIST     = 0.10    # m   – hard stop reverse
US_STALE_SECS    = 1.0     # s

# ── Pipeline topics ───────────────────────────────────────────────────────────
INPUT_TOPIC  = "/cmd_vel_smoothed"
OUTPUT_TOPIC = "/cmd_vel"
LIDAR_TOPIC  = "/scan_corrected"
DEPTH_TOPIC  = "/ascamera_hp60c/camera_publisher/depth0/points"

# ── Beep ──────────────────────────────────────────────────────────────────────
BEEP_COOLDOWN = 2.0        # s


def _vel_scale(dist: float, stop: float, slow: float) -> float:
    """Linear scale in [0.0, 1.0]: 0 at or below stop, 1 at or above slow."""
    if dist <= stop:
        return 0.0
    if dist >= slow:
        return 1.0
    return (dist - stop) / (slow - stop)


def _beep():
    def _run():
        try:
            import sounddevice as sd
            sr  = 16000
            t   = np.linspace(0, 0.12, int(sr * 0.12))
            hi  = (np.sin(2 * np.pi * 1000 * t) * 0.6).astype(np.float32)
            lo  = (np.sin(2 * np.pi * 700  * t) * 0.6).astype(np.float32)
            gap = np.zeros(int(sr * 0.04), dtype=np.float32)
            sd.play(np.concatenate([hi, gap, lo]), samplerate=sr, blocking=True)
        except Exception:
            pass
    threading.Thread(target=_run, daemon=True).start()


class SafetyShield(Node):

    def __init__(self):
        super().__init__("safety_shield")

        # ── Internal state (all guarded by _lock) ─────────────────────────────
        # Lidar
        self._lidar_fwd_dist = math.inf   # nearest obstacle in corridor
        self._lidar_blocked  = False       # dist < LIDAR_STOP_DIST
        self._lidar_slowing  = False       # in slow zone, not yet stopped

        # Depth
        self._depth_fwd_dist = math.inf
        self._depth_blocked  = False
        self._depth_slowing  = False

        # Ultrasonic
        self._us_front_blocked = False
        self._us_rear_blocked  = False
        self._us_dists = {'fl': math.inf, 'fr': math.inf,
                          'bl': math.inf, 'br': math.inf}

        self._last_lidar = 0.0
        self._last_depth = 0.0
        self._last_us    = 0.0
        self._last_beep  = 0.0
        self._lock       = threading.Lock()

        # ── Callback groups ────────────────────────────────────────────────────
        self._sensor_group = MutuallyExclusiveCallbackGroup()
        self._gate_group   = MutuallyExclusiveCallbackGroup()

        # ── QoS ───────────────────────────────────────────────────────────────
        sensor_qos = QoSProfile(
            reliability=ReliabilityPolicy.BEST_EFFORT,
            history=HistoryPolicy.KEEP_LAST,
            depth=1,
        )

        # ── Subscriptions ──────────────────────────────────────────────────────
        self.create_subscription(
            LaserScan, LIDAR_TOPIC, self._on_scan, sensor_qos,
            callback_group=self._sensor_group)
        self.create_subscription(
            PointCloud2, DEPTH_TOPIC, self._on_cloud, sensor_qos,
            callback_group=self._sensor_group)
        self.create_subscription(
            Range, '/us/front_left',  lambda m: self._on_us(m, 'fl'), sensor_qos,
            callback_group=self._sensor_group)
        self.create_subscription(
            Range, '/us/front_right', lambda m: self._on_us(m, 'fr'), sensor_qos,
            callback_group=self._sensor_group)
        self.create_subscription(
            Range, '/us/back_left',   lambda m: self._on_us(m, 'bl'), sensor_qos,
            callback_group=self._sensor_group)
        self.create_subscription(
            Range, '/us/back_right',  lambda m: self._on_us(m, 'br'), sensor_qos,
            callback_group=self._sensor_group)

        self.create_subscription(Twist, INPUT_TOPIC, self._on_cmd, 1,
                                 callback_group=self._gate_group)
        self._pub = self.create_publisher(Twist, OUTPUT_TOPIC, 10)

        self.get_logger().info(
            f"[SafetyShield] ready – graduated response\n"
            f"  lidar : slow < {LIDAR_SLOW_DIST:.2f} m | "
            f"stop < {LIDAR_STOP_DIST:.2f} m | ±{LIDAR_WIDTH_HALF:.2f} m corridor\n"
            f"  depth : slow < {DEPTH_SLOW_DIST:.2f} m | "
            f"stop < {DEPTH_STOP_DIST:.2f} m | ±{DEPTH_WIDTH_HALF:.2f} m corridor\n"
            f"  US fwd: stop < {US_FRONT_DIST:.2f} m (FL/FR)  "
            f"rear: stop < {US_REAR_DIST:.2f} m (BL/BR)\n"
            f"  {INPUT_TOPIC} -> {OUTPUT_TOPIC}"
        )

    # ── Lidar callback ────────────────────────────────────────────────────────

    def _on_scan(self, msg: LaserScan):
        self._last_lidar = time.monotonic()

        n = len(msg.ranges)
        if n == 0:
            return

        angles = (np.arange(n) * msg.angle_increment + msg.angle_min).astype(np.float32)
        ranges = np.array(msg.ranges, dtype=np.float32)

        valid = (
            np.isfinite(ranges)
            & (ranges > msg.range_min)
            & (ranges < msg.range_max)
        )

        x = ranges * np.cos(angles)   # forward in laser frame
        y = ranges * np.sin(angles)   # lateral

        # Check full slow zone so we get distance for graduated scaling
        in_corridor = (
            valid
            & (x > 0.10)
            & (x < LIDAR_SLOW_DIST)
            & (np.abs(y) < LIDAR_WIDTH_HALF)
        )

        n_pts = int(np.sum(in_corridor))
        fwd_dist = float(x[in_corridor].min()) if n_pts >= LIDAR_MIN_PTS else math.inf

        hard_blocked = fwd_dist <= LIDAR_STOP_DIST
        slowing      = (not hard_blocked) and (fwd_dist < LIDAR_SLOW_DIST)

        with self._lock:
            prev_blocked = self._lidar_blocked
            prev_slowing = self._lidar_slowing
            self._lidar_fwd_dist = fwd_dist
            self._lidar_blocked  = hard_blocked
            self._lidar_slowing  = slowing

        if hard_blocked and not prev_blocked:
            self.get_logger().warn(
                f"[SafetyShield] *** STOP (lidar) *** {fwd_dist:.2f} m ({n_pts} pts)")
            self._maybe_beep()
        elif not hard_blocked and prev_blocked:
            self.get_logger().info("[SafetyShield] forward clear (lidar)")
        elif slowing and not prev_slowing:
            self.get_logger().info(
                f"[SafetyShield] slowing (lidar) obstacle at {fwd_dist:.2f} m")
        elif not slowing and prev_slowing and not hard_blocked:
            self.get_logger().info("[SafetyShield] slow zone clear (lidar)")

    # ── Depth camera callback ──────────────────────────────────────────────────

    def _on_cloud(self, msg: PointCloud2):
        self._last_depth = time.monotonic()

        n    = msg.width * msg.height
        step = msg.point_step
        if n == 0 or step == 0 or step % 4 != 0:
            return

        offs = {f.name: f.offset for f in msg.fields}
        if not all(k in offs for k in ("x", "y", "z")):
            return

        floats_per_pt = step // 4
        arr = np.frombuffer(msg.data, dtype=np.float32)
        if len(arr) < n * floats_per_pt:
            return
        arr = arr[:n * floats_per_pt].reshape(n, floats_per_pt)

        x = arr[:, offs["x"] // 4]
        y = arr[:, offs["y"] // 4]
        z = arr[:, offs["z"] // 4]   # optical frame: Z = depth forward

        valid = np.isfinite(x) & np.isfinite(y) & np.isfinite(z)

        # Check full slow zone for graduated scaling
        in_corridor = (
            valid
            & (z >  0.05)
            & (z <  DEPTH_SLOW_DIST)
            & (np.abs(x) < DEPTH_WIDTH_HALF)
            & (y >  DEPTH_HEIGHT_MIN)
            & (y <  DEPTH_HEIGHT_MAX)
        )

        n_slow = int(np.sum(in_corridor))
        fwd_dist = float(z[in_corridor].min()) if n_slow >= DEPTH_SLOW_PTS else math.inf

        # Hard stop requires stricter point count
        n_stop = int(np.sum(in_corridor & (z < DEPTH_STOP_DIST)))
        hard_blocked = (fwd_dist <= DEPTH_STOP_DIST) and (n_stop >= DEPTH_MIN_PTS)
        slowing      = (not hard_blocked) and (fwd_dist < DEPTH_SLOW_DIST)

        with self._lock:
            prev_blocked = self._depth_blocked
            prev_slowing = self._depth_slowing
            self._depth_fwd_dist = fwd_dist
            self._depth_blocked  = hard_blocked
            self._depth_slowing  = slowing

        if hard_blocked and not prev_blocked:
            self.get_logger().warn(
                f"[SafetyShield] *** STOP (camera) *** {fwd_dist:.2f} m ({n_stop} pts)")
            self._maybe_beep()
        elif not hard_blocked and prev_blocked:
            self.get_logger().info("[SafetyShield] forward clear (camera)")
        elif slowing and not prev_slowing:
            self.get_logger().info(
                f"[SafetyShield] slowing (camera) obstacle at {fwd_dist:.2f} m")
        elif not slowing and prev_slowing and not hard_blocked:
            self.get_logger().info("[SafetyShield] slow zone clear (camera)")

    # ── Ultrasonic callbacks ───────────────────────────────────────────────────

    def _on_us(self, msg: Range, sensor: str):
        self._last_us = time.monotonic()
        dist = msg.range if math.isfinite(msg.range) else math.inf

        with self._lock:
            self._us_dists[sensor] = dist
            prev_f = self._us_front_blocked
            prev_r = self._us_rear_blocked
            self._us_front_blocked = (
                min(self._us_dists['fl'], self._us_dists['fr']) < US_FRONT_DIST)
            self._us_rear_blocked = (
                min(self._us_dists['bl'], self._us_dists['br']) < US_REAR_DIST)
            new_f = self._us_front_blocked
            new_r = self._us_rear_blocked
            d     = dict(self._us_dists)

        if new_f and not prev_f:
            self.get_logger().warn(
                f"[SafetyShield] *** STOP (ultrasonic fwd) *** "
                f"FL={d['fl']:.2f} m  FR={d['fr']:.2f} m")
            self._maybe_beep()
        elif not new_f and prev_f:
            self.get_logger().info("[SafetyShield] forward clear (ultrasonic)")

        if new_r and not prev_r:
            self.get_logger().warn(
                f"[SafetyShield] *** STOP (ultrasonic rear) *** "
                f"BL={d['bl']:.2f} m  BR={d['br']:.2f} m")
            self._maybe_beep()
        elif not new_r and prev_r:
            self.get_logger().info("[SafetyShield] rear clear (ultrasonic)")

    # ── Velocity gate ──────────────────────────────────────────────────────────

    def _on_cmd(self, msg: Twist):
        now          = time.monotonic()
        lidar_stale  = (now - self._last_lidar) > LIDAR_STALE_SECS
        depth_stale  = (now - self._last_depth) > DEPTH_STALE_SECS
        us_stale     = (now - self._last_us)    > US_STALE_SECS

        with self._lock:
            lidar_dist     = self._lidar_fwd_dist if not lidar_stale else math.inf
            depth_dist     = self._depth_fwd_dist if not depth_stale else math.inf
            us_fwd_blocked = not us_stale and self._us_front_blocked
            us_rear_blocked = not us_stale and self._us_rear_blocked

        fwd_scale = min(
            _vel_scale(lidar_dist, LIDAR_STOP_DIST, LIDAR_SLOW_DIST),
            _vel_scale(depth_dist, DEPTH_STOP_DIST, DEPTH_SLOW_DIST),
            0.0 if us_fwd_blocked else 1.0,
        )

        lin = msg.linear.x
        if lin > 0.0:
            lin *= fwd_scale
        if us_rear_blocked and lin < 0.0:
            lin = 0.0

        out = Twist()
        out.linear.x  = lin
        out.angular.z = msg.angular.z
        self._pub.publish(out)

    # ── Helpers ────────────────────────────────────────────────────────────────

    def _maybe_beep(self):
        now = time.monotonic()
        if now - self._last_beep >= BEEP_COOLDOWN:
            self._last_beep = now
            _beep()


def main(args=None):
    rclpy.init(args=args)
    node = SafetyShield()
    executor = MultiThreadedExecutor(num_threads=2)
    executor.add_node(node)
    try:
        executor.spin()
    except KeyboardInterrupt:
        pass
    finally:
        try:
            node._pub.publish(Twist())
        except Exception:
            pass
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
