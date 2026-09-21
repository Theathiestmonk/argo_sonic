#!/usr/bin/env python3
"""
goal_approach_limiter.py – halve nav speed over the last metre to the goal.

There is no MPPI parameter for this. The "slow down as you approach the
goal" knob people remember from Nav2 — use_approach_linear_velocity_scaling
/ approach_velocity_scaling_dist — belongs to Regulated Pure Pursuit, not to
nav2_mppi_controller; MPPI's GoalCritic only starts *weighting* the goal
inside threshold_to_consider, it never caps speed. So MPPI cruises at vx_max
right up to xy_goal_tolerance and then brakes as hard as ax_min allows.

What MPPI does have is the standard Nav2 speed-limit interface:
controller_server subscribes to /speed_limit (nav2_msgs/SpeedLimit) and
Optimizer::setSpeedLimit rescales vx_max/wz_max off their base values live.
That is what this node drives:

    remaining > APPROACH_DIST (1.00 m) : 100 %  ->  vx_max 0.50 m/s
    remaining <= APPROACH_DIST         :  50 %  ->  vx_max 0.25 m/s

Going through /speed_limit rather than just multiplying /cmd_vel downstream
is the whole point. safety_shield scales the controller's output behind its
back, which is fine for an obstacle (the shield knows something Nav2 does
not) but wrong here: MPPI would keep planning 0.50 m/s trajectories, the
speed would step down in a single tick, and — the same blind spot the
progress_checker comment in nav2.yaml complains about — Nav2 would have no
idea why it is suddenly moving at half the speed it asked for. Published as
a speed limit instead, MPPI re-plans against the lower cap itself and
velocity_smoother ramps into it at max_decel. That ramp is what makes the
arrival smooth rather than a jolt at the 1 m line.

Note that setSpeedLimit scales wz_max by the same ratio, so turns in the
last metre also halve (0.30 -> 0.15 rad/s). That is wanted here — this robot
works close to tables — but it is why the limit is released the moment the
plan goes away rather than being left latched.

Remaining distance is measured *along* the global plan from the robot's
closest point on it, not straight-line to the goal, so a goal 0.8 m away
through a wall does not trip the slow zone while the robot is still driving
the long way around.
"""

import math

import rclpy
import tf2_ros
from nav2_msgs.msg import SpeedLimit
from nav_msgs.msg import Path
from rclpy.node import Node

# ── Approach zone ─────────────────────────────────────────────────────────────
APPROACH_DIST  = 1.00   # m – at or under this remaining distance, run limited
# Released a little further out than it engages. Without the gap, a robot
# hovering either side of 1.00 m (or plan-to-plan jitter in the remaining
# length, which is recomputed from scratch on every replan) would flip the
# limit on and off every cycle and MPPI would keep re-clamping its control
# sequence. 0.15 m is comfortably wider than replan jitter.
RELEASE_DIST   = 1.15   # m – must be > APPROACH_DIST
APPROACH_PCT   = 50.0   # % of base vx_max/wz_max inside the zone
FULL_PCT       = 100.0  # % – no limit

# ── Plan tracking ─────────────────────────────────────────────────────────────
PLAN_TOPIC     = "/plan"
SPEED_TOPIC    = "/speed_limit"
BASE_FRAME     = "base_link"
# No plan for this long means nav is idle — goal reached, cancelled, or
# failed. The limit MUST be released here: nothing else will do it, and a
# latched 50 % would silently halve the robot for the rest of the session
# (including teleop, which goes through the same controller cap).
PLAN_STALE_SECS = 2.0
CHECK_HZ        = 10.0
# Re-send the current limit this often even when unchanged, so a
# controller_server that restarted mid-run (it comes back up at its base
# constraints) picks the limit back up instead of cruising the last metre.
HEARTBEAT_SECS  = 2.0


def _remaining_along_path(path: Path, rx: float, ry: float) -> float:
    """Path length from the pose on `path` closest to (rx, ry) through to its
    final pose. Returns inf for a plan too short to measure."""
    poses = path.poses
    n = len(poses)
    if n < 2:
        return math.inf

    xs = [p.pose.position.x for p in poses]
    ys = [p.pose.position.y for p in poses]

    nearest = 0
    best = math.inf
    for i in range(n):
        d = (xs[i] - rx) ** 2 + (ys[i] - ry) ** 2
        if d < best:
            best = d
            nearest = i

    total = 0.0
    for i in range(nearest, n - 1):
        total += math.hypot(xs[i + 1] - xs[i], ys[i + 1] - ys[i])
    return total


class GoalApproachLimiter(Node):

    def __init__(self):
        super().__init__("goal_approach_limiter")

        self._plan = None
        self._last_plan_time = 0.0
        self._limited = False          # currently inside the approach zone
        self._last_sent_pct = None
        self._last_sent_time = 0.0

        self._tf_buffer = tf2_ros.Buffer()
        self._tf_listener = tf2_ros.TransformListener(self._tf_buffer, self)

        self.create_subscription(Path, PLAN_TOPIC, self._on_plan, 10)
        self._pub = self.create_publisher(SpeedLimit, SPEED_TOPIC, 10)

        self.create_timer(1.0 / CHECK_HZ, self._tick)

        self.get_logger().info(
            f"[GoalApproach] ready – {PLAN_TOPIC} -> {SPEED_TOPIC}\n"
            f"  <= {APPROACH_DIST:.2f} m remaining : {APPROACH_PCT:.0f} % speed\n"
            f"  >  {RELEASE_DIST:.2f} m remaining : {FULL_PCT:.0f} % speed"
        )

        # Start from a known state rather than inheriting whatever limit a
        # previous run of this node left set on controller_server.
        self._send(FULL_PCT, force=True)

    # ── Plan callback ─────────────────────────────────────────────────────────

    def _on_plan(self, msg: Path):
        self._plan = msg
        self._last_plan_time = self._now()

    # ── Main loop ─────────────────────────────────────────────────────────────

    def _tick(self):
        now = self._now()

        if self._plan is None or (now - self._last_plan_time) > PLAN_STALE_SECS:
            if self._limited:
                self.get_logger().info(
                    "[GoalApproach] plan gone (arrived/cancelled) – full speed")
            self._limited = False
            self._send(FULL_PCT)
            return

        robot = self._robot_xy(self._plan.header.frame_id)
        if robot is None:
            # No TF yet. Leave the limit exactly as it is rather than guessing
            # — releasing here would speed the robot back up mid-approach.
            return

        remaining = _remaining_along_path(self._plan, robot[0], robot[1])
        if not math.isfinite(remaining):
            return

        if not self._limited and remaining <= APPROACH_DIST:
            self._limited = True
            self.get_logger().info(
                f"[GoalApproach] {remaining:.2f} m to goal – "
                f"limiting to {APPROACH_PCT:.0f} %")
        elif self._limited and remaining > RELEASE_DIST:
            self._limited = False
            self.get_logger().info(
                f"[GoalApproach] {remaining:.2f} m to goal – full speed")

        self._send(APPROACH_PCT if self._limited else FULL_PCT)

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _robot_xy(self, frame: str):
        if not frame:
            return None
        try:
            tf = self._tf_buffer.lookup_transform(
                frame, BASE_FRAME, rclpy.time.Time())
        except Exception:
            return None
        return tf.transform.translation.x, tf.transform.translation.y

    def _send(self, pct: float, force: bool = False):
        now = self._now()
        if (not force and pct == self._last_sent_pct
                and (now - self._last_sent_time) < HEARTBEAT_SECS):
            return
        msg = SpeedLimit()
        msg.header.stamp = self.get_clock().now().to_msg()
        msg.percentage = True
        msg.speed_limit = pct
        self._pub.publish(msg)
        self._last_sent_pct = pct
        self._last_sent_time = now

    def _now(self) -> float:
        return self.get_clock().now().nanoseconds / 1e9


def main(args=None):
    rclpy.init(args=args)
    node = GoalApproachLimiter()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        # Never leave controller_server clamped on the way out.
        try:
            node._send(FULL_PCT, force=True)
        except Exception:
            pass
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
