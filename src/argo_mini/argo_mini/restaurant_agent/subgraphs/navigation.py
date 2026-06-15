"""
Navigation subgraph.

Completely isolated from LLM reasoning.
The LLM only sets destination in entities; this subgraph executes Nav2.

Handles: navigation_request, return_home, kitchen_request
"""

import logging
import threading
from langgraph.graph import StateGraph, START, END
from ..state import AgentState
from ..integrations.nav2 import Nav2Client

logger = logging.getLogger("argo_nav")

# Global Nav2 client — injected at startup by the ROS node
_nav2_client: Nav2Client | None = None


def set_nav2_client(client: Nav2Client):
    global _nav2_client
    _nav2_client = client


def navigation_node(state: AgentState) -> dict:
    """
    Resolve destination → send Nav2 goal → report result via TTS.

    Navigation runs in a background thread so the robot can speak
    "heading to table 3" immediately without blocking.
    """
    global _nav2_client
    intent = state.get("current_intent", "")
    entities = state.get("entities", {})

    # ── Resolve destination ───────────────────────────────────────────────────
    if intent == "return_home":
        destination = "home"
    elif intent == "kitchen_request":
        destination = "kitchen"
    elif intent == "navigation_request":
        table = entities.get("table")
        if table:
            destination = f"table_{table}"
        else:
            return {
                "response": "Which table should I navigate to?",
                "next_action": "",
                "task_status": "idle",
            }
    else:
        # Fallback: use destination already in state
        destination = state.get("destination", "home")

    if _nav2_client is None:
        # Nav2 not connected — simulation mode
        logger.warning("[NAV] Nav2 client not connected — simulating")
        label = destination.replace("_", " ").title()
        return {
            "destination": destination,
            "navigation_status": "arrived",
            "response": f"Heading to {label}!",
            "next_action": "",
            "task_status": "completed",
        }

    resolved = _nav2_client.resolve_destination(destination)
    if not resolved:
        return {
            "response": f"I don't know where {destination} is. Please check the waypoints.",
            "navigation_status": "failed",
            "task_status": "failed",
        }

    label = resolved.replace("_", " ").title()

    # Speak immediately, navigate in background
    def _nav_bg():
        result = _nav2_client.navigate_to(resolved)
        logger.info(f"[NAV] {resolved} → {result}")

    threading.Thread(target=_nav_bg, daemon=True).start()

    return {
        "destination": resolved,
        "navigation_status": "navigating",
        "response": f"On my way to {label}!",
        "next_action": "",
        "active_subgraph": "",
        "task_status": "active",
    }


def build_navigation_subgraph(ros_node=None) -> StateGraph:
    if ros_node is not None:
        set_nav2_client(Nav2Client(ros_node))
    sg = StateGraph(AgentState)
    sg.add_node("navigate", navigation_node)
    sg.add_edge(START, "navigate")
    sg.add_edge("navigate", END)
    return sg.compile()
