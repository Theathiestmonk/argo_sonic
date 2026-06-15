"""
Task router node — decides which subgraph to enter.

Priority:
1. Emergency → always emergency_graph
2. next_action set → route to active_subgraph (mid-conversation)
3. intent → route to matching subgraph
"""

from ..state import AgentState


def task_router_node(state: AgentState) -> dict:
    """Pass-through node; routing logic lives in route_to_subgraph()."""
    return {}


def route_to_subgraph(state: AgentState) -> str:
    """Conditional edge function for the main graph."""

    # 1. Emergency always wins
    if state.get("current_intent") == "emergency":
        return "emergency_graph"
    if state.get("emergency_type", "none") != "none":
        return "emergency_graph"

    # 2. Mid-conversation: route back to the active subgraph
    next_action = state.get("next_action", "")
    active_sg = state.get("active_subgraph", "")
    if next_action.startswith("await_") and active_sg:
        return f"{active_sg}_graph"

    # 3. Fresh intent routing
    intent = state.get("current_intent", "general_chat")
    _map = {
        "greeting":           "greeting_graph",
        "place_order":        "order_graph",
        "modify_order":       "order_graph",
        "cancel_order":       "order_graph",
        "request_bill":       "billing_graph",
        "navigation_request": "navigation_graph",
        "return_home":        "navigation_graph",
        "kitchen_request":    "navigation_graph",
        "general_chat":       "chat_graph",
        "help":               "greeting_graph",
        "emergency":          "emergency_graph",
    }
    return _map.get(intent, "chat_graph")
