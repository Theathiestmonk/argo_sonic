#!/usr/bin/env python3
"""
safety_shield.py – Unified velocity safety gate for Argo Mini.

Pipeline:
    /cmd_vel_smoothed  –>  [safety_shield]  –>  /cmd_vel

Three independent sensor sources with graduated forward-speed response:

  Lidar (LaserScan)  – /scan_corrected
    > LIDAR_SLOW_DIST (0.70 m) : full speed — lowered from 1.00 m; that
      margin throttled speed well below vx_max any time something was
      within a full metre of the forward path, common indoors
    LIDAR_STOP_DIST .. LIDAR_SLOW_DIST : speed scaled linearly 0→100%
    < LIDAR_STOP_DIST (0.40 m) : hard stop — raised back up for more
      stopping margin, taking priority over the final-approach-abort
      tradeoff that came with lowering it (see git history)

  Depth camera (PointCloud2)  – raw /points (independent of restamper)
    > DEPTH_SLOW_DIST (0.55 m) : full speed — lowered from 0.80 m, same reasoning
    DEPTH_STOP_DIST .. DEPTH_SLOW_DIST : speed scaled linearly 0→100%
    < DEPTH_STOP_DIST (0.25 m) : hard stop — lowered from 0.40 m, same reasoning

  Ultrasonic (Range x 4)  – binary, no slow zone needed at 40 cm
    FL/FR < US_FRONT_DIST (0.40 m) : hard stop forward
    BL/BR < US_REAR_DIST  (0.40 m) : hard stop reverse

Gate:
    fwd_scale = min(lidar_scale, depth_scale, us_scale)   ∈ [0.0, 1.0]
    linear.x  = commanded_linear.x * fwd_scale            (forward only)
    reverse   = blocked by US rear (binary)
    angular.z = always passes through

All sensors stale → full pass-through (fail-open, robot never freezes).
"""

import math
import os
import socket
import subprocess
import threading
import time
from pathlib import Path

import numpy as np
import rclpy
from geometry_msgs.msg import Twist
from rclpy.callback_groups import MutuallyExclusiveCallbackGroup
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node
from rclpy.qos import HistoryPolicy, QoSProfile, ReliabilityPolicy
from sensor_msgs.msg import LaserScan, PointCloud2, Range

# ── Lidar ─────────────────────────────────────────────────────────────────────
# Lowered from 1.00 — at 1.00m, anything within a full metre of the
# robot's forward path (very common in a compact indoor space) throttled
# speed down well below vx_max, capping overall speed independent of
# whatever the nav stack's own limits allowed. Still a real graduated
# zone before the hard stop below, just narrower.
LIDAR_SLOW_DIST  = 0.70    # m   – begin speed reduction
# Raised back to 0.40 — explicitly asked for more margin here, taking
# priority over the earlier "final approach was landing inside this
# margin" concern (see git history) — that goal-abort tradeoff is
# accepted in exchange for stopping farther from an obstacle.
LIDAR_STOP_DIST  = 0.40    # m   – hard stop
LIDAR_WIDTH_HALF = 0.35    # m   – half-width of danger corridor
LIDAR_MIN_PTS    = 3       # minimum scan points to register an obstacle
LIDAR_STALE_SECS = 1.0     # s   – treat as stale if no scan arrives

# ── Depth camera ──────────────────────────────────────────────────────────────
DEPTH_SLOW_DIST  = 0.55    # m   – begin speed reduction — lowered from 0.80, same reasoning as LIDAR_SLOW_DIST above
DEPTH_STOP_DIST  = 0.25    # m   – hard stop — lowered from 0.40, same reasoning as LIDAR_STOP_DIST above
DEPTH_SLOW_PTS   = 5       # minimum pts to activate slow zone
DEPTH_MIN_PTS    = 15      # minimum pts for hard stop (noise filter)
DEPTH_WIDTH_HALF = 0.40    # m   – half-width of danger corridor
DEPTH_HEIGHT_MIN = -1.30   # opt Y – lower bound (ignore floor)
DEPTH_HEIGHT_MAX =  0.05   # opt Y – upper bound (ignore ceiling)
DEPTH_STALE_SECS = 1.0     # s

# ── Ultrasonic ────────────────────────────────────────────────────────────────
US_FRONT_DIST    = 0.40    # m   – hard stop forward
US_REAR_DIST     = 0.40    # m   – hard stop reverse
US_STALE_SECS    = 1.0     # s

# ── Graduated-speed smoothing ─────────────────────────────────────────────────
# _vel_scale() used to run straight off each new lidar/depth reading with zero
# smoothing, so a few cm of ordinary sensor noise near the slow-zone boundary
# translated directly into the commanded speed surging/easing every tick —
# "oscillating, then moving" instead of one smooth ramp. DIST_FILTER_ALPHA
# exponentially smooths the distance fed to the graduated scale only; the
# hard-stop trigger itself always uses the raw, unfiltered reading (see
# _on_scan/_on_cloud) so an actual closing obstacle is never stopped for late.
DIST_FILTER_ALPHA = 0.35  # weight on the newest reading; lower = smoother but slower to react

# ── Pipeline topics ───────────────────────────────────────────────────────────
INPUT_TOPIC  = "/cmd_vel_smoothed"
OUTPUT_TOPIC = "/cmd_vel"
LIDAR_TOPIC  = "/scan_corrected"
DEPTH_TOPIC  = "/ascamera_hp60c/camera_publisher/depth0/points"

# ── Beep ──────────────────────────────────────────────────────────────────────
BEEP_COOLDOWN = 2.0        # s

# Same hardware auto-detect voice_agent.py uses for its speaker device — the
# Jetson's USB audio card isn't the ALSA default, so mpg123 needs pointing at
# it explicitly there; a dev machine just uses whatever "default" resolves to.
_ON_JETSON     = os.path.exists("/etc/nv_tegra_release") or "argo" in socket.gethostname().lower()
SPEAKER_DEVICE = "plughw:CARD=Device,DEV=0" if _ON_JETSON else "default"

# <repo_root>/sound/sound.mp3 — resolved from this file's own location (not a
# hardcoded absolute path) so it keeps working under a --symlink-install
# build, same convention as waypoint_manager.py's DEFAULT_WAYPOINTS_DIR.
SOUND_FILE = str(Path(__file__).resolve().parent.parent.parent.parent / "sound" / "sound.mp3")

# This is a "something's nearby" nudge, not an alarm — kept well under full
# volume on purpose so it doesn't disturb the room (customers/staff), and
# fixed in code rather than following the Jetson's system mixer, since
# voice_agent.py/tts.py already play speech at whatever that's set to and
# this alert needs to stay quiet independent of that. Raised from 0.60 —
# reported inaudible on real hardware; re-lower this if it turns out to be
# genuinely disruptive rather than just quiet.
ALERT_VOLUME = 0.85   # 0.0-1.0, applied to both the mp3 clip and the fallback tone


def _vel_scale(dist: float, stop: float, slow: float) -> float:
    """Linear scale in [0.0, 1.0]: 0 at or below stop, 1 at or above slow."""
    if dist <= stop:
        return 0.0
    if dist >= slow:
        return 1.0
    return (dist - stop) / (slow - stop)


def _scale_for(raw_dist: float, smooth_dist: float, stop: float, slow: float) -> float:
    """Hard stop always reacts to the raw, unfiltered reading — never delayed
    by smoothing. Otherwise the graduated scale runs off the smoothed
    distance, damping frame-to-frame sensor noise out of the slow-zone ramp."""
    if raw_dist <= stop:
        return 0.0
    return _vel_scale(smooth_dist, stop, slow)


def _play_alert_clip() -> bool:
    """Play SOUND_FILE via mpg123. Returns False if the file or the mpg123
    binary aren't there, or playback fails, so the caller can fall back to
    the synthesized tone — the alert must still fire either way. Logs the
    actual failure reason (previously silently swallowed, which made a
    "the beep isn't audible" report undebuggable — no way to tell whether
    it was just quiet, hitting the wrong output device, or not running at
    all)."""
    if not os.path.isfile(SOUND_FILE):
        print(f"[SafetyShield] alert clip missing: {SOUND_FILE}")
        return False
    try:
        # mpg123's -f scale is independent of the ALSA mixer (default full
        # scale = 32768) — this is what actually keeps the alert quiet
        # regardless of whatever system volume voice/TTS are using.
        scale = int(32768 * ALERT_VOLUME)
        result = subprocess.run(
            ["mpg123", "-q", "-a", SPEAKER_DEVICE, "-f", str(scale), SOUND_FILE],
            timeout=5, capture_output=True, text=True,
        )
        if result.returncode != 0:
            print(f"[SafetyShield] mpg123 failed (device={SPEAKER_DEVICE!r}, "
                  f"rc={result.returncode}): {result.stderr.strip()[-300:]}")
            return False
        return True
    except FileNotFoundError:
        print("[SafetyShield] mpg123 not installed — falling back to synth beep")
        return False
    except Exception as e:
        print(f"[SafetyShield] alert clip playback error: {e}")
        return False


def _play_synth_beep():
    """Two-tone chirp fallback — used whenever the recorded clip can't play."""
    try:
        import sounddevice as sd
        sr  = 16000
        t   = np.linspace(0, 0.12, int(sr * 0.12))
        hi  = (np.sin(2 * np.pi * 1000 * t) * ALERT_VOLUME).astype(np.float32)
        lo  = (np.sin(2 * np.pi * 700  * t) * ALERT_VOLUME).astype(np.float32)
        gap = np.zeros(int(sr * 0.04), dtype=np.float32)
        sd.play(np.concatenate([hi, gap, lo]), samplerate=sr, blocking=True)
    except Exception as e:
        print(f"[SafetyShield] synth beep fallback ALSO failed — no alert sound played: {e}")


def _beep():
    def _run():
        if not _play_alert_clip():
            _play_synth_beep()
    threading.Thread(target=_run, daemon=True).start()


class SafetyShield(Node):

    def __init__(self):
        super().__init__("safety_shield")

        # ── Internal state (all guarded by _lock) ─────────────────────────────
        # Lidar
        self._lidar_fwd_dist = math.inf          # raw — drives the hard-stop trigger, never filtered
        self._lidar_fwd_dist_smooth = math.inf   # EMA'd — drives the graduated slow-zone scale only
        self._lidar_blocked  = False       # dist < LIDAR_STOP_DIST
        self._lidar_slowing  = False       # in slow zone, not yet stopped

        # Depth
        self._depth_fwd_dist = math.inf
        self._depth_fwd_dist_smooth = math.inf
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
            # Snap instead of blend across an inf<->finite transition (an
            # obstacle newly appearing/disappearing) — EMA-blending against
            # inf would otherwise stay pinned at inf forever. Otherwise EMA
            # smooths ordinary frame-to-frame noise between two real readings.
            if math.isinf(self._lidar_fwd_dist_smooth) or math.isinf(fwd_dist):
                self._lidar_fwd_dist_smooth = fwd_dist
            else:
                self._lidar_fwd_dist_smooth = (
                    DIST_FILTER_ALPHA * fwd_dist + (1 - DIST_FILTER_ALPHA) * self._lidar_fwd_dist_smooth)
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
            if math.isinf(self._depth_fwd_dist_smooth) or math.isinf(fwd_dist):
                self._depth_fwd_dist_smooth = fwd_dist
            else:
                self._depth_fwd_dist_smooth = (
                    DIST_FILTER_ALPHA * fwd_dist + (1 - DIST_FILTER_ALPHA) * self._depth_fwd_dist_smooth)
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
            lidar_dist        = self._lidar_fwd_dist if not lidar_stale else math.inf
            lidar_dist_smooth = self._lidar_fwd_dist_smooth if not lidar_stale else math.inf
            depth_dist        = self._depth_fwd_dist if not depth_stale else math.inf
            depth_dist_smooth = self._depth_fwd_dist_smooth if not depth_stale else math.inf
            us_fwd_blocked = not us_stale and self._us_front_blocked
            us_rear_blocked = not us_stale and self._us_rear_blocked

        fwd_scale = min(
            _scale_for(lidar_dist, lidar_dist_smooth, LIDAR_STOP_DIST, LIDAR_SLOW_DIST),
            _scale_for(depth_dist, depth_dist_smooth, DEPTH_STOP_DIST, DEPTH_SLOW_DIST),
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
