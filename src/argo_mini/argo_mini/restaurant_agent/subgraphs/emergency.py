"""
Emergency subgraph.

Triggers: collision detected, low battery, human assistance request, "stop".
Immediately stops all navigation and notifies operator.
"""

import logging
import threading
from langgraph.graph import StateGraph, START, END
from ..state import AgentState

logger = logging.getLogger("argo_emergency")

# Injected by the ROS node
_ros_node = None
_nav2_client = None


def set_ros_node(node, nav2_client=None):
    global _ros_node, _nav2_client
    _ros_node = node
    _nav2_client = nav2_client


def emergency_node(state: AgentState) -> dict:
    etype = state.get("emergency_type", "none")
    text = state.get("user_input", "").lower()

    # Determine emergency type from input if not already set
    if etype == "none":
        if "battery" in text:
            etype = "low_battery"
        elif "collision" in text or "crash" in text:
            etype = "collision"
        else:
            etype = "help_requested"

    logger.warning(f"[EMERGENCY] Type: {etype}")

    # 1. Cancel navigation immediately
    if _nav2_client is not None:
        try:
            _nav2_client.cancel_navigation()
        except Exception as e:
            logger.error(f"[EMERGENCY] Cancel nav failed: {e}")

    # 2. Publish emergency alert on ROS topic
    if _ros_node is not None:
        try:
            from std_msgs.msg import String
            if not hasattr(_ros_node, "_emergency_pub"):
                _ros_node._emergency_pub = _ros_node.create_publisher(
                    String, "/robot/emergency", 10
                )
            import json
            msg = String()
            msg.data = json.dumps({"type": etype, "session": state.get("session_id")})
            _ros_node._emergency_pub.publish(msg)
        except Exception as e:
            logger.error(f"[EMERGENCY] ROS publish failed: {e}")

    responses = {
        "low_battery":   "My battery is low! I am stopping now. Please plug me in for charging.",
        "collision":     "Obstacle detected! I have stopped for safety. A human will assist shortly.",
        "help_requested":"Stopping all tasks. Calling a human waiter for assistance. Please wait.",
    }
    response = responses.get(etype, "Emergency stop! All tasks halted. Staff has been notified.")

    return {
        "emergency_type": etype,
        "task_status": "failed",
        "navigation_status": "idle",
        "next_action": "",
        "active_subgraph": "",
        "response": response,
    }


def build_emergency_subgraph(ros_node=None, nav2_client=None) -> StateGraph:
    if ros_node is not None:
        set_ros_node(ros_node, nav2_client)
    sg = StateGraph(AgentState)
    sg.add_node("emergency", emergency_node)
    sg.add_edge(START, "emergency")
    sg.add_edge("emergency", END)
    return sg.compile()
