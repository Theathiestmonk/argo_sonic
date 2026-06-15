"""
Greeting subgraph.

Triggers: customer detected, customer initiates conversation, 'help' intent.
"""

from langgraph.graph import StateGraph, START, END
from ..state import AgentState


def greeting_node(state: AgentState) -> dict:
    name = state.get("customer_name", "")
    hour = __import__("datetime").datetime.now().hour
    if hour < 12:
        time_greeting = "Good morning"
    elif hour < 17:
        time_greeting = "Good afternoon"
    else:
        time_greeting = "Good evening"

    if name:
        response = (f"{time_greeting}, {name}! Welcome back to Argo Kitchen. "
                    "How can I assist you today?")
    else:
        response = (f"{time_greeting}! Welcome to Argo Kitchen. "
                    "I am Argo Sonic, your robot waiter. "
                    "I can take your order, deliver food, or call a waiter. "
                    "How may I help you?")

    return {
        "response": response,
        "next_action": "",
        "active_subgraph": "",
        "task_status": "idle",
    }


def build_greeting_subgraph() -> StateGraph:
    sg = StateGraph(AgentState)
    sg.add_node("greet", greeting_node)
    sg.add_edge(START, "greet")
    sg.add_edge("greet", END)
    return sg.compile()
