"""
Main LangGraph restaurant agent graph.

Architecture:
  START
    → intent_classifier   (LLM intent extraction or fast-path)
    → task_router         (pass-through; routing in conditional edges)
    → [subgraph]          (greeting / order / navigation / billing / chat / emergency)
    → response_node       (record conversation history)
  END

Each subgraph sets state["response"] which is spoken by TTS in the ROS node.
Multi-turn state (next_action, active_subgraph) persists between invocations.
"""

from langgraph.graph import StateGraph, START, END

from .state import AgentState
from .nodes.intent import intent_classifier_node
from .nodes.router import task_router_node, route_to_subgraph
from .nodes.response import response_node
from .subgraphs.greeting import build_greeting_subgraph
from .subgraphs.ordering import build_ordering_subgraph
from .subgraphs.navigation import build_navigation_subgraph
from .subgraphs.billing import build_billing_subgraph
from .subgraphs.general_chat import build_chat_subgraph
from .subgraphs.emergency import build_emergency_subgraph

_SUBGRAPH_NAMES = [
    "greeting_graph",
    "order_graph",
    "navigation_graph",
    "billing_graph",
    "chat_graph",
    "emergency_graph",
]


def build_graph(ros_node=None):
    """
    Build and compile the main restaurant agent graph.

    Args:
        ros_node: Optional ROS2 node instance for Nav2 + emergency publishers.

    Returns:
        Compiled LangGraph (CompiledGraph) ready for .invoke()
    """
    nav2_client = None

    # Build subgraphs
    greeting_sg   = build_greeting_subgraph()
    order_sg      = build_ordering_subgraph()
    nav_sg        = build_navigation_subgraph(ros_node)
    billing_sg    = build_billing_subgraph()
    chat_sg       = build_chat_subgraph()

    # Pass nav2_client to emergency subgraph too
    from .subgraphs.navigation import _nav2_client as nav2_ref
    emergency_sg  = build_emergency_subgraph(ros_node, nav2_ref)

    # ── Main graph ─────────────────────────────────────────────────────────────
    graph = StateGraph(AgentState)

    graph.add_node("intent_classifier", intent_classifier_node)
    graph.add_node("task_router",       task_router_node)
    graph.add_node("greeting_graph",    greeting_sg)
    graph.add_node("order_graph",       order_sg)
    graph.add_node("navigation_graph",  nav_sg)
    graph.add_node("billing_graph",     billing_sg)
    graph.add_node("chat_graph",        chat_sg)
    graph.add_node("emergency_graph",   emergency_sg)
    graph.add_node("response",          response_node)

    # Entry → intent → router
    graph.add_edge(START, "intent_classifier")
    graph.add_edge("intent_classifier", "task_router")

    # Router → subgraph (conditional)
    graph.add_conditional_edges(
        "task_router",
        route_to_subgraph,
        {name: name for name in _SUBGRAPH_NAMES},
    )

    # All subgraphs → response → END
    for name in _SUBGRAPH_NAMES:
        graph.add_edge(name, "response")
    graph.add_edge("response", END)

    return graph.compile()
