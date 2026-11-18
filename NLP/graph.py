"""The LangGraph agent.

        ┌────────────────────────────── sleep / farewell ◀──────────┐
        ▼                                                           │
 START → wake → greet → listen ─┬─(silence)→ silence_prompt ─▶ listen
                          ▲      └─▶ understand ─▶ route ─┬▶ navigation ┐
                          │                              ├▶ ordering   │
                          │                              ├▶ movement   ├─▶ listen
                          │                              ├▶ menu ──(yes, order it)──▶ ordering
                          │                              ├▶ general    │
                          └──────────────────────────────┴▶ unclear ───┘

One graph run = the robot's whole life: it loops wake → conversation → sleep → wake … forever.
Every node is async; speech is only *queued* by nodes (voice.say / say_stream), so work and talking overlap,
and `listen` is the single place that waits for the speaker to finish.
"""
from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from .nodes.core import make_core
from .nodes.ctx import Ctx
from .nodes.general import make_general
from .nodes.menu import make_menu
from .nodes.movement import make_movement
from .nodes.navigation import make_navigation
from .nodes.ordering import make_ordering
from .state import State


def build_graph(c: Ctx):
    core = make_core(c)
    g = StateGraph(State)
    for name in ("wake", "greet", "listen", "silence_prompt", "understand", "unclear", "farewell", "sleep"):
        g.add_node(name, core[name])
    g.add_node("navigation", make_navigation(c))
    g.add_node("ordering", make_ordering(c))
    g.add_node("movement", make_movement(c))
    g.add_node("menu", make_menu(c))
    g.add_node("general", make_general(c))

    g.add_edge(START, "wake")
    g.add_edge("wake", "greet")
    g.add_edge("greet", "listen")
    g.add_conditional_edges("listen", core["after_listen"], ["silence_prompt", "understand", "sleep"])
    g.add_edge("silence_prompt", "listen")
    g.add_conditional_edges("understand", core["route"],
                            ["navigation", "ordering", "movement", "menu", "general", "farewell", "unclear"])
    for n in ("navigation", "ordering", "movement", "general", "unclear"):
        g.add_edge(n, "listen")
    g.add_conditional_edges("menu", lambda s: "ordering" if s.get("intent") == "ordering" else "listen",
                            ["ordering", "listen"])
    g.add_edge("farewell", "wake")
    g.add_edge("sleep", "wake")
    return g.compile()
