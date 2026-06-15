"""
ROS2 Nav2 navigation integration.

Sends NavigateToPose action goals and tracks result.
Waypoints are loaded from data/waypoints.json.
"""

import json
import logging
import math
import os
import threading
from typing import Optional, Callable

logger = logging.getLogger("argo_nav2")

_DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")


def _load_waypoints(path: str = "") -> dict:
    p = path or os.path.join(_DATA_DIR, "waypoints.json")
    with open(p) as f:
        return json.load(f)["waypoints"]


def _yaw_to_quaternion(yaw: float) -> dict:
    return {
        "x": 0.0, "y": 0.0,
        "z": math.sin(yaw / 2),
        "w": math.cos(yaw / 2),
    }


class Nav2Client:
    """
    Thin wrapper around ROS2 Nav2 NavigateToPose action.

    Usage:
        client = Nav2Client(ros_node)
        status = client.navigate_to("table_3")   # blocks until done or timeout
        # returns: "arrived" | "failed" | "unknown"
    """

    def __init__(self, ros_node=None, waypoints_path: str = ""):
        self._node = ros_node
        self._waypoints = _load_waypoints(waypoints_path)
        self._action_client = None
        self._result_event = threading.Event()
        self._last_result = "unknown"

        if ros_node is not None:
            self._init_action_client()

    def _init_action_client(self):
        try:
            from nav2_msgs.action import NavigateToPose
            from rclpy.action import ActionClient
            self._action_client = ActionClient(
                self._node, NavigateToPose, "navigate_to_pose"
            )
            logger.info("[NAV2] Action client initialized")
        except Exception as e:
            logger.warning(f"[NAV2] Could not init action client: {e}")

    def navigate_to(
        self,
        destination: str,
        timeout_sec: float = 60.0,
        on_feedback: Optional[Callable] = None,
    ) -> str:
        """
        Navigate to a named destination and block until done.

        Args:
            destination: key in waypoints.json (e.g. "table_3", "kitchen", "home")
            timeout_sec: give up after this many seconds
            on_feedback: optional callback(distance_remaining)

        Returns:
            "arrived" | "failed" | "no_waypoint" | "no_nav2"
        """
        wp = self._waypoints.get(destination)
        if wp is None:
            logger.warning(f"[NAV2] Unknown destination: {destination}")
            return "no_waypoint"

        if self._action_client is None:
            logger.warning("[NAV2] No action client — running in simulation mode")
            return "arrived"   # sim pass-through

        try:
            from nav2_msgs.action import NavigateToPose
            from geometry_msgs.msg import PoseStamped
            import rclpy

            if not self._action_client.wait_for_server(timeout_sec=5.0):
                logger.error("[NAV2] NavigateToPose server not available")
                return "failed"

            goal = NavigateToPose.Goal()
            goal.pose = PoseStamped()
            goal.pose.header.frame_id = "map"
            goal.pose.header.stamp = self._node.get_clock().now().to_msg()
            goal.pose.pose.position.x = float(wp["x"])
            goal.pose.pose.position.y = float(wp["y"])
            goal.pose.pose.position.z = 0.0
            q = _yaw_to_quaternion(float(wp.get("yaw", 0.0)))
            goal.pose.pose.orientation.x = q["x"]
            goal.pose.pose.orientation.y = q["y"]
            goal.pose.pose.orientation.z = q["z"]
            goal.pose.pose.orientation.w = q["w"]

            self._result_event.clear()
            self._last_result = "navigating"
            logger.info(f"[NAV2] Sending goal → {destination} ({wp['label']})")

            future = self._action_client.send_goal_async(
                goal, feedback_callback=lambda fb: self._on_feedback(fb, on_feedback)
            )
            rclpy.spin_until_future_complete(self._node, future, timeout_sec=10.0)

            goal_handle = future.result()
            if not goal_handle or not goal_handle.accepted:
                logger.error("[NAV2] Goal rejected")
                return "failed"

            result_future = goal_handle.get_result_async()
            rclpy.spin_until_future_complete(
                self._node, result_future, timeout_sec=timeout_sec
            )

            result = result_future.result()
            if result:
                from action_msgs.msg import GoalStatus
                if result.status == GoalStatus.STATUS_SUCCEEDED:
                    logger.info(f"[NAV2] Arrived at {destination}")
                    return "arrived"
                else:
                    logger.warning(f"[NAV2] Navigation failed (status={result.status})")
                    return "failed"
            return "failed"

        except Exception as e:
            logger.error(f"[NAV2] Navigation error: {e}")
            return "failed"

    def _on_feedback(self, feedback_msg, callback):
        try:
            dist = feedback_msg.feedback.distance_remaining
            logger.debug(f"[NAV2] Distance remaining: {dist:.2f}m")
            if callback:
                callback(dist)
        except Exception:
            pass

    def cancel_navigation(self):
        """Cancel any active navigation goal."""
        if self._action_client:
            try:
                self._action_client._cancel_goal_async()
                logger.info("[NAV2] Navigation cancelled")
            except Exception as e:
                logger.warning(f"[NAV2] Cancel failed: {e}")

    def resolve_destination(self, intent_destination: str) -> str:
        """
        Map intent entity to waypoint key.
        e.g. "table 3" → "table_3", "kitchen" → "kitchen", "home" → "home"
        """
        d = intent_destination.lower().strip()
        d = d.replace(" ", "_").replace("-", "_")
        if d in self._waypoints:
            return d
        # Try "table" prefix
        import re
        m = re.search(r'(\d+)', d)
        if m:
            key = f"table_{m.group(1)}"
            if key in self._waypoints:
                return key
        return ""

    def list_destinations(self) -> list:
        return list(self._waypoints.keys())
