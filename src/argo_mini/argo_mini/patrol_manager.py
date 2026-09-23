#!/usr/bin/env python3
"""
patrol_manager.py – drive a goal <-> home shuttle until told to stop.

Click a point on the live map with the Patrol tool and the robot drives
there, comes back to wherever it was standing when patrol started, and
repeats, until Stop Patrol.

Topics:
    /patrol/start   geometry_msgs/PoseStamped  – the point to patrol to
    /patrol/stop    std_msgs/Empty             – stop after cancelling the current leg
    /patrol/status  std_msgs/String (JSON)     – state for the UI

The loop lives here rather than in the browser on purpose. The dashboard's
own arrival detection (App.jsx sendNavGoal) is a proximity poll on /pose
with a 90 s "assume we made it" fallback — fine for a single trip a human
is watching, wrong for an unattended loop: every lap would inherit that
guess, and closing the tab, refreshing, or a dropped websocket would strand
the robot mid-patrol with no one left running the loop. Here the legs are
real NavigateToPose goals, so a lap advances on the action's actual result
code, a failed leg is a real failure, and the patrol keeps running (or
stops cleanly) regardless of what the UI is doing.

"Home" is captured from TF the moment patrol starts — the pose the robot
was standing at, not a named waypoint and not the map origin. It is kept
for the whole patrol, including through a re-target, so the shuttle always
returns to the same place.

While a patrol is active this node owns navigate_to_pose. Anything else
sending a goal (a table trip, the Set Goal tool, waypoint_manager) will
fight it for the action server, which is why the dashboard hides those
controls while patrol is running.
"""

import json
import math
import time

import rclpy
import tf2_ros
from action_msgs.msg import GoalStatus
from geometry_msgs.msg import PoseStamped
from nav2_msgs.action import NavigateToPose
from rclpy.action import ActionClient
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node
from std_msgs.msg import Empty, String

# ── Behaviour ─────────────────────────────────────────────────────────────────
DWELL_SECS       = 2.0   # s – pause at each end before turning around
# A leg is retried rather than failing the patrol outright: Nav2 aborts for
# plenty of recoverable reasons (a person standing in the corridor, a
# momentary localization wobble), and an unattended patrol that quits the
# first time someone walks past is useless. After this many consecutive
# failures on the SAME leg it does stop, and says why in /patrol/status —
# at that point something is actually wrong (goal inside an obstacle,
# unreachable room) and retrying forever would just grind.
MAX_LEG_RETRIES  = 2
RETRY_DELAY_SECS = 3.0
SERVER_WAIT_SECS = 8.0

# ── Topics ────────────────────────────────────────────────────────────────────
START_TOPIC  = "/patrol/start"
STOP_TOPIC   = "/patrol/stop"
STATUS_TOPIC = "/patrol/status"
NAV_ACTION   = "navigate_to_pose"
BASE_FRAME   = "base_link"
DEFAULT_FRAME = "map"

TICK_HZ        = 5.0
# The UI subscribes with plain (volatile) QoS through rosbridge, so it never
# sees a latched message. A steady heartbeat is what lets a freshly loaded
# or refreshed page find out a patrol is already running, instead of showing
# an idle button while the robot is driving itself back and forth.
HEARTBEAT_SECS = 1.0


class PatrolManager(Node):

    def __init__(self):
        super().__init__("patrol_manager")

        self._group = ReentrantCallbackGroup()

        # ── Patrol state ──────────────────────────────────────────────────────
        self._active = False
        self._goal_pose = None      # PoseStamped – the clicked point
        self._home_pose = None      # PoseStamped – where patrol started
        self._leg = "idle"          # idle | to_goal | to_home | dwell
        self._next_leg = None       # which leg the dwell is counting down to
        self._laps = 0              # completed goal->home round trips
        self._error = None
        self._dwell_until = 0.0
        self._retries = 0

        # ── In-flight NavigateToPose ──────────────────────────────────────────
        self._handle = None         # accepted goal handle, if any
        self._nav_pending = False   # a goal is sent and not yet resolved
        self._result_status = None  # set by the result callback, read by _tick
        self._distance = None       # latest feedback, surfaced in status

        self._tf_buffer = tf2_ros.Buffer()
        self._tf_listener = tf2_ros.TransformListener(self._tf_buffer, self)

        self._nav = ActionClient(self, NavigateToPose, NAV_ACTION,
                                 callback_group=self._group)

        self.create_subscription(PoseStamped, START_TOPIC, self._on_start, 10,
                                 callback_group=self._group)
        self.create_subscription(Empty, STOP_TOPIC, self._on_stop, 10,
                                 callback_group=self._group)
        self._status_pub = self.create_publisher(String, STATUS_TOPIC, 10)

        self.create_timer(1.0 / TICK_HZ, self._tick, callback_group=self._group)
        self.create_timer(HEARTBEAT_SECS, self._publish_status,
                          callback_group=self._group)

        self.get_logger().info(
            f"[Patrol] ready – {START_TOPIC} / {STOP_TOPIC} -> {STATUS_TOPIC}\n"
            f"  dwell {DWELL_SECS:.1f} s at each end, {MAX_LEG_RETRIES} retries per leg")
        self._publish_status()

    # ── Commands ──────────────────────────────────────────────────────────────

    def _on_start(self, msg: PoseStamped):
        goal = PoseStamped()
        goal.header.frame_id = msg.header.frame_id or DEFAULT_FRAME
        goal.pose = msg.pose

        if self._active:
            # Re-target. Keep the original home — the robot is somewhere
            # mid-lap right now, so capturing "here" as home would anchor the
            # patrol to an arbitrary point in the corridor.
            self.get_logger().info(
                f"[Patrol] re-target -> ({goal.pose.position.x:.2f}, "
                f"{goal.pose.position.y:.2f}), home unchanged")
            self._goal_pose = goal
            self._cancel_current()
            self._begin_leg("to_goal")
            return

        home = self._current_pose(goal.header.frame_id)
        if home is None:
            self._error = f"no {goal.header.frame_id} -> {BASE_FRAME} transform"
            self.get_logger().error(f"[Patrol] cannot start: {self._error}")
            self._publish_status()
            return

        self._active = True
        self._goal_pose = goal
        self._home_pose = home
        self._laps = 0
        self._retries = 0
        self._error = None
        self.get_logger().info(
            f"[Patrol] start – home ({home.pose.position.x:.2f}, "
            f"{home.pose.position.y:.2f}) <-> goal "
            f"({goal.pose.position.x:.2f}, {goal.pose.position.y:.2f})")
        self._begin_leg("to_goal")

    def _on_stop(self, _msg: Empty):
        if not self._active:
            return
        self.get_logger().info(f"[Patrol] stop – {self._laps} lap(s) completed")
        self._error = None   # a clean stop clears any earlier leg failure
        self._cancel_current()
        self._go_idle()

    # ── State machine ─────────────────────────────────────────────────────────

    def _tick(self):
        if not self._active:
            return

        if self._leg == "dwell":
            if time.monotonic() >= self._dwell_until:
                self._begin_leg(self._next_leg)
            return

        if self._result_status is None:
            return

        status = self._result_status
        self._result_status = None
        self._nav_pending = False
        self._handle = None

        if status == GoalStatus.STATUS_SUCCEEDED:
            self._retries = 0
            if self._leg == "to_goal":
                self.get_logger().info("[Patrol] reached goal – heading home")
                self._start_dwell("to_home")
            else:
                self._laps += 1
                self.get_logger().info(
                    f"[Patrol] back home – lap {self._laps} complete")
                self._start_dwell("to_goal")
            return

        if status == GoalStatus.STATUS_CANCELED:
            # Either _on_stop cancelled it (already idle, nothing to do) or a
            # re-target did (a new leg is already under way).
            return

        self._retries += 1
        if self._retries > MAX_LEG_RETRIES:
            where = "the goal" if self._leg == "to_goal" else "home"
            self.get_logger().error(
                f"[Patrol] giving up – {self._leg} failed {self._retries} "
                f"times (Nav2 status {status})")
            # Set before _go_idle so the single status it publishes already
            # carries the reason — the UI reads this straight out of
            # /patrol/status to say why the patrol ended.
            self._error = f"Patrol stopped: could not reach {where}"
            self._go_idle()
            return

        self.get_logger().warn(
            f"[Patrol] {self._leg} failed (status {status}) – "
            f"retry {self._retries}/{MAX_LEG_RETRIES}")
        self._start_dwell(self._leg, delay=RETRY_DELAY_SECS)

    def _start_dwell(self, next_leg: str, delay: float = DWELL_SECS):
        self._leg = "dwell"
        self._next_leg = next_leg
        self._dwell_until = time.monotonic() + delay
        self._publish_status()

    def _begin_leg(self, leg: str):
        target = self._goal_pose if leg == "to_goal" else self._home_pose
        if target is None:
            self._go_idle()
            return
        self._leg = leg
        self._distance = None
        self._publish_status()
        self._send_nav_goal(target)

    def _go_idle(self):
        self._active = False
        self._leg = "idle"
        self._next_leg = None
        self._nav_pending = False
        self._result_status = None
        self._distance = None
        self._retries = 0
        self._publish_status()

    # ── NavigateToPose plumbing ───────────────────────────────────────────────

    def _send_nav_goal(self, pose: PoseStamped):
        if not self._nav.wait_for_server(timeout_sec=SERVER_WAIT_SECS):
            self._error = "navigate_to_pose action server not available"
            self.get_logger().error(f"[Patrol] {self._error}")
            self._go_idle()
            return

        goal = NavigateToPose.Goal()
        goal.pose = PoseStamped()
        goal.pose.header.frame_id = pose.header.frame_id or DEFAULT_FRAME
        goal.pose.header.stamp = self.get_clock().now().to_msg()
        goal.pose.pose = pose.pose

        self._nav_pending = True
        self._result_status = None
        future = self._nav.send_goal_async(goal, feedback_callback=self._on_feedback)
        future.add_done_callback(self._on_goal_response)

    def _on_goal_response(self, future):
        try:
            handle = future.result()
        except Exception as e:
            self.get_logger().error(f"[Patrol] send_goal failed: {e}")
            self._result_status = GoalStatus.STATUS_ABORTED
            return
        if not handle.accepted:
            self.get_logger().warn("[Patrol] goal rejected by Nav2")
            self._result_status = GoalStatus.STATUS_ABORTED
            return
        self._handle = handle
        handle.get_result_async().add_done_callback(self._on_result)

    def _on_result(self, future):
        try:
            self._result_status = future.result().status
        except Exception as e:
            self.get_logger().error(f"[Patrol] result failed: {e}")
            self._result_status = GoalStatus.STATUS_ABORTED

    def _on_feedback(self, msg):
        self._distance = float(msg.feedback.distance_remaining)

    def _cancel_current(self):
        if self._handle is not None:
            try:
                self._handle.cancel_goal_async()
            except Exception:
                pass
        self._handle = None
        self._nav_pending = False
        self._result_status = None

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _current_pose(self, frame: str):
        try:
            tf = self._tf_buffer.lookup_transform(
                frame or DEFAULT_FRAME, BASE_FRAME, rclpy.time.Time())
        except Exception:
            return None
        p = PoseStamped()
        p.header.frame_id = frame or DEFAULT_FRAME
        p.pose.position.x = tf.transform.translation.x
        p.pose.position.y = tf.transform.translation.y
        p.pose.orientation = tf.transform.rotation
        return p

    def _publish_status(self):
        def xy(p):
            if p is None:
                return None
            return {"x": round(p.pose.position.x, 3),
                    "y": round(p.pose.position.y, 3)}

        state = {
            "active": self._active,
            "leg": self._leg,
            "next_leg": self._next_leg,
            "laps": self._laps,
            "goal": xy(self._goal_pose),
            "home": xy(self._home_pose),
            "distance_remaining": (round(self._distance, 2)
                                   if self._distance is not None
                                   and math.isfinite(self._distance) else None),
            "error": self._error,
        }
        self._status_pub.publish(String(data=json.dumps(state)))


def main(args=None):
    rclpy.init(args=args)
    node = PatrolManager()
    executor = MultiThreadedExecutor(num_threads=4)
    executor.add_node(node)
    try:
        executor.spin()
    except KeyboardInterrupt:
        pass
    finally:
        # Don't leave a leg running with nothing left to advance the loop.
        try:
            node._cancel_current()
        except Exception:
            pass
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
