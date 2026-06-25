#!/usr/bin/env python3
"""
safety_shield.py – Unified velocity safety gate for Argo Mini.

Pipeline:
    /cmd_vel_smoothed  –>  [safety_shield]  –>  /cmd_vel

Two independent sensor sources, each with its own stale timeout:

  Depth camera (PointCloud2)
    – Obstacle in forward corridor closer than DEPTH_STOP_DIST -> block forward
    – Camera stale -> camera vote cleared (US still active)

  Ultrasonic sensors (Range x 4, published by serial_bridge from ESP32)
    – FL or FR closer than US_FRONT_DIST -> block forward
    – BL or BR closer than US_REAR_DIST  -> block reverse
    – US stale -> US vote cleared (camera still active)

Gate logic:
    forward blocked  = (depth_blocked  OR  us_front_blocked)
    reverse blocked  = us_rear_blocked
    angular.z        = always passes through (robot can always rotate away)

Both sensors stale -> full pass-through so robot never freezes.
"""

import math
import threading
import time

import numpy as np
import rclpy
from geometry_msgs.msg import Twist
from rclpy.node import Node
from rclpy.qos import HistoryPolicy, QoSProfile, ReliabilityPolicy
from sensor_msgs.msg import PointCloud2, Range

# ── Depth camera ──────────────────────────────────────────────────────────────
DEPTH_STOP_DIST  = 0.60    # m   – stop if person/obstacle enters forward corridor
DEPTH_WIDTH_HALF = 0.40    # m   – half-width of danger corridor (~robot width)
DEPTH_HEIGHT_MIN = -1.30   # opt Y – ignore above robot head
DEPTH_HEIGHT_MAX =  0.05   # opt Y – ignore floor returns
DEPTH_MIN_PTS    = 15      # minimum cloud points to confirm obstacle (noise filter)
DEPTH_STALE_SECS = 1.0     # s   – clear camera vote if no frame arrives

# ── Ultrasonic ────────────────────────────────────────────────────────────────
US_FRONT_DIST    = 0.10    # m   – last-resort block (10 cm from sensor face)
US_REAR_DIST     = 0.10    # m   – block reverse at 10 cm
US_STALE_SECS    = 1.0     # s   – clear US vote if no Range message arrives

# ── Pipeline topics ───────────────────────────────────────────────────────────
INPUT_TOPIC  = "/cmd_vel_smoothed"
OUTPUT_TOPIC = "/cmd_vel"
DEPTH_TOPIC  = "/ascamera_hp60c/camera_publisher/depth0/points"

# ── Beep ──────────────────────────────────────────────────────────────────────
BEEP_COOLDOWN = 2.0        # s   – minimum gap between consecutive beeps


def _beep():
    """Fire-and-forget two-tone warning beep."""
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
        self._depth_blocked     = False
        self._us_front_blocked  = False
        self._us_rear_blocked   = False
        self._us_dists          = {'fl': math.inf, 'fr': math.inf,
                                   'bl': math.inf, 'br': math.inf}

        self._last_depth  = 0.0   # time.monotonic() of last PointCloud2
        self._last_us     = 0.0   # time.monotonic() of last Range message
        self._last_beep   = 0.0
        self._lock        = threading.Lock()

        # ── QoS for sensor topics ──────────────────────────────────────────────
        sensor_qos = QoSProfile(
            reliability=ReliabilityPolicy.BEST_EFFORT,
            history=HistoryPolicy.KEEP_LAST,
            depth=1,
        )

        # ── Subscriptions ──────────────────────────────────────────────────────
        self.create_subscription(
            PointCloud2, DEPTH_TOPIC, self._on_cloud, sensor_qos)

        self.create_subscription(
            Range, '/us/front_left',  lambda m: self._on_us(m, 'fl'), sensor_qos)
        self.create_subscription(
            Range, '/us/front_right', lambda m: self._on_us(m, 'fr'), sensor_qos)
        self.create_subscription(
            Range, '/us/back_left',   lambda m: self._on_us(m, 'bl'), sensor_qos)
        self.create_subscription(
            Range, '/us/back_right',  lambda m: self._on_us(m, 'br'), sensor_qos)

        self.create_subscription(Twist, INPUT_TOPIC, self._on_cmd, 10)
        self._pub = self.create_publisher(Twist, OUTPUT_TOPIC, 10)

        self.get_logger().info(
            f"[SafetyShield] ready\n"
            f"  depth  : stop < {DEPTH_STOP_DIST:.2f} m | "
            f"corridor ±{DEPTH_WIDTH_HALF:.2f} m | min {DEPTH_MIN_PTS} pts\n"
            f"  US fwd : stop < {US_FRONT_DIST:.2f} m (FL / FR)\n"
            f"  US rear: stop < {US_REAR_DIST:.2f}  m (BL / BR)\n"
            f"  {INPUT_TOPIC} -> {OUTPUT_TOPIC}"
        )

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

        valid  = np.isfinite(x) & np.isfinite(y) & np.isfinite(z)
        danger = (
            valid
            & (z >  0.05)                     # skip camera self-returns
            & (z <  DEPTH_STOP_DIST)          # within stop distance
            & (np.abs(x) < DEPTH_WIDTH_HALF)  # within robot corridor
            & (y > DEPTH_HEIGHT_MIN)           # not above robot
            & (y < DEPTH_HEIGHT_MAX)           # not below floor
        )

        n_pts   = int(np.sum(danger))
        blocked = n_pts >= DEPTH_MIN_PTS

        with self._lock:
            prev = self._depth_blocked
            self._depth_blocked = blocked
            min_z = float(z[danger].min()) if (blocked and n_pts) else 0.0

        if blocked and not prev:
            self.get_logger().warn(
                f"[SafetyShield] *** FORWARD BLOCKED (camera) *** "
                f"obstacle at {min_z:.2f} m ({n_pts} pts)"
            )
            self._maybe_beep()
        elif not blocked and prev:
            self.get_logger().info("[SafetyShield] forward clear (camera)")

    # ── Ultrasonic callbacks ───────────────────────────────────────────────────

    def _on_us(self, msg: Range, sensor: str):
        self._last_us = time.monotonic()
        dist = msg.range if math.isfinite(msg.range) else math.inf

        with self._lock:
            self._us_dists[sensor] = dist

            prev_f = self._us_front_blocked
            prev_r = self._us_rear_blocked

            self._us_front_blocked = (
                min(self._us_dists['fl'], self._us_dists['fr']) < US_FRONT_DIST
            )
            self._us_rear_blocked = (
                min(self._us_dists['bl'], self._us_dists['br']) < US_REAR_DIST
            )

            new_f = self._us_front_blocked
            new_r = self._us_rear_blocked
            d     = dict(self._us_dists)

        if new_f and not prev_f:
            self.get_logger().warn(
                f"[SafetyShield] *** FORWARD BLOCKED (ultrasonic) *** "
                f"FL={d['fl']:.2f} m  FR={d['fr']:.2f} m"
            )
            self._maybe_beep()
        elif not new_f and prev_f:
            self.get_logger().info("[SafetyShield] forward clear (ultrasonic)")

        if new_r and not prev_r:
            self.get_logger().warn(
                f"[SafetyShield] *** REAR BLOCKED (ultrasonic) *** "
                f"BL={d['bl']:.2f} m  BR={d['br']:.2f} m"
            )
            self._maybe_beep()
        elif not new_r and prev_r:
            self.get_logger().info("[SafetyShield] rear clear (ultrasonic)")

    # ── Velocity gate ──────────────────────────────────────────────────────────

    def _on_cmd(self, msg: Twist):
        now         = time.monotonic()
        depth_stale = (now - self._last_depth) > DEPTH_STALE_SECS
        us_stale    = (now - self._last_us)    > US_STALE_SECS

        with self._lock:
            fwd_blocked = (
                (not depth_stale and self._depth_blocked)
                or (not us_stale  and self._us_front_blocked)
            )
            rear_blocked = not us_stale and self._us_rear_blocked

        lin = msg.linear.x
        if fwd_blocked  and lin > 0.0:
            lin = 0.0
        if rear_blocked and lin < 0.0:
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
    try:
        rclpy.spin(node)
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
