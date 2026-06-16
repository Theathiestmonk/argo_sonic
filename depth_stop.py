#!/usr/bin/env python3
"""
depth_stop.py ? Instant depth-camera obstacle brake.

Sits in the velocity pipeline:
    /cmd_vel_smoothed  ?  [depth_stop]  ?  /cmd_vel

Rules:
  ? Any obstacle closer than STOP_DIST in the forward zone ? zero velocity immediately
  ? Reverse (linear.x < 0) is always allowed so robot can back away
  ? If depth camera stops publishing (stale > 0.5s) ? pass through (fail-safe)

Run after sourcing ROS:
    source /opt/ros/humble/setup.bash
    source ~/argo_mini_ws/install/setup.bash
    python3 depth_stop.py
"""

import random, subprocess, threading, time
import numpy as np
import rclpy
from rclpy.node import Node
from rclpy.qos import QoSProfile, ReliabilityPolicy, HistoryPolicy
from geometry_msgs.msg import Twist
from sensor_msgs.msg import PointCloud2

# ?? Tune these ????????????????????????????????????????????????????????????????
STOP_DIST   = 0.60   # metres ? stop if anything closer than this
WIDTH_HALF  = 0.30   # metres ? half-width of danger corridor (robot ~0.30m wide)
HEIGHT_MIN  = -1.30  # optical-frame Y ? head (~1.7m) at 0.30m stop distance
HEIGHT_MAX  =  0.05  # optical-frame Y ? floor appears at +0.29 with 27� upward tilt, excluded
MIN_POINTS  = 5      # need this many danger-zone points to trigger (kills noise)
STALE_SECS  = 0.5    # if no depth frame for this long, pass through commands

INPUT_TOPIC  = "/cmd_vel_smoothed"
OUTPUT_TOPIC = "/cmd_vel"
DEPTH_TOPIC  = "/ascamera_hp60c/camera_publisher/depth0/points"

PIPER_BIN   = "/home/argo/piper/piper"
PIPER_MODEL = "/home/argo/piper-voices/en_US-ryan-medium.onnx"
TTS_COOLDOWN = 4.0   # seconds between obstacle voice alerts

_OBSTACLE_PHRASES = [
    "Hey, please give me some side.",
    "Excuse me, could you please move?",
    "Pardon me, I need to pass through.",
    "Please step aside, coming through!",
    "Sorry, can you give me some space?",
    "Hey there, please make way!",
    "Could you move a little? Thank you!",
    "Beep beep, please give me some room.",
]
# ??????????????????????????????????????????????????????????????????????????????


def _say(text: str):
    """Play TTS phrase via Piper in a fire-and-forget thread."""
    def _run():
        try:
            import sounddevice as sd
            proc = subprocess.Popen(
                [PIPER_BIN, "--model", PIPER_MODEL, "--output-raw"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            )
            raw, _ = proc.communicate(input=text.encode(), timeout=10)
            if raw:
                import numpy as np
                audio = np.frombuffer(raw, dtype=np.int16)
                sd.play(audio, samplerate=22050, blocking=True)
        except Exception:
            pass
    threading.Thread(target=_run, daemon=True).start()


class DepthStop(Node):

    def __init__(self):
        super().__init__("depth_stop")

        self._blocked      = False
        self._last_cloud   = 0.0
        self._last_tts     = 0.0
        self._lock         = threading.Lock()

        # Depth topic needs BEST_EFFORT to match camera publisher QoS
        sensor_qos = QoSProfile(
            reliability=ReliabilityPolicy.BEST_EFFORT,
            history=HistoryPolicy.KEEP_LAST,
            depth=1,
        )

        self.create_subscription(PointCloud2, DEPTH_TOPIC,  self._on_cloud, sensor_qos)
        self.create_subscription(Twist,       INPUT_TOPIC,  self._on_cmd,   10)
        self._pub = self.create_publisher(Twist, OUTPUT_TOPIC, 10)

        self.get_logger().info(
            f"[DepthStop] ready ? stop at {STOP_DIST}m | "
            f"zone �{WIDTH_HALF}m wide | min {MIN_POINTS} pts\n"
            f"  {INPUT_TOPIC} ? {OUTPUT_TOPIC}"
        )

    # ?? depth cloud ???????????????????????????????????????????????????????????

    def _on_cloud(self, msg: PointCloud2):
        self._last_cloud = time.monotonic()

        n    = msg.width * msg.height
        step = msg.point_step          # bytes per point

        if n == 0 or step == 0:
            return

        # Get byte offsets for x, y, z from message header
        offs = {f.name: f.offset for f in msg.fields}
        if not all(k in offs for k in ("x", "y", "z")):
            return

        # Fast extraction: view raw bytes as float32 matrix (n_pts � floats_per_point)
        # Works when point_step is divisible by 4 (true for all standard XYZ clouds)
        floats_per_pt = step // 4
        arr = np.frombuffer(msg.data, dtype=np.float32)
        if len(arr) < n * floats_per_pt:
            return
        arr = arr[: n * floats_per_pt].reshape(n, floats_per_pt)

        x_col = offs["x"] // 4
        y_col = offs["y"] // 4
        z_col = offs["z"] // 4

        x = arr[:, x_col]
        y = arr[:, y_col]
        z = arr[:, z_col]

        # --- Danger zone filter ---
        # Optical frame: Z = depth forward, X = right, Y = down
        valid  = np.isfinite(x) & np.isfinite(y) & np.isfinite(z)
        danger = (
            valid
            & (z >  0.05)             # skip camera self-returns
            & (z <  STOP_DIST)        # within stop distance
            & (np.abs(x) < WIDTH_HALF) # within robot width
            & (y > HEIGHT_MIN)         # not above robot
            & (y < HEIGHT_MAX)         # not below floor level
        )

        n_pts   = int(np.sum(danger))
        blocked = n_pts >= MIN_POINTS

        with self._lock:
            prev = self._blocked
            self._blocked = blocked

        if blocked and not prev:
            min_z = float(z[danger].min()) if n_pts else 0.0
            self.get_logger().warn(
                f"[DepthStop] *** STOP *** obstacle at {min_z:.2f}m ({n_pts} pts)"
            )
            now = time.monotonic()
            if now - self._last_tts >= TTS_COOLDOWN:
                self._last_tts = now
                _say(random.choice(_OBSTACLE_PHRASES))
        elif not blocked and prev:
            self.get_logger().info("[DepthStop] clear ? resuming")

    # ?? velocity gate ?????????????????????????????????????????????????????????

    def _on_cmd(self, msg: Twist):
        stale = (time.monotonic() - self._last_cloud) > STALE_SECS

        if stale:
            # Camera not publishing ? pass through so robot isn't frozen
            self._pub.publish(msg)
            return

        with self._lock:
            blocked = self._blocked

        if blocked and msg.linear.x > 0.01:
            # Forward blocked ? hard zero, but allow reverse to back away
            self._pub.publish(Twist())
        else:
            self._pub.publish(msg)


def main():
    rclpy.init()
    node = DepthStop()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        # Send stop before exiting
        try:
            node._pub.publish(Twist())
        except Exception:
            pass
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
