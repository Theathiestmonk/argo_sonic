"""Graph state. Small on purpose – it is copied on every super-step."""
from __future__ import annotations

from typing import Any, Optional, TypedDict


class State(TypedDict, total=False):
    # ---- per-turn
    utterance: str
    intent: str                 # navigation | ordering | movement | menu | general | end | unclear
    ent: dict[str, Any]         # all entities extracted in the single classification call
    # ---- dialogue memory (what the robot last asked -> disambiguates the next reply)
    awaiting: Optional[str]
    silent_turns: int
    resumed: bool               # graph restarted by barge-in: keep the cart, only reset the dialogue
    # ---- navigation
    location: Optional[str]     # waypoint the robot is currently at
    nav_suggestion: Optional[str]
    # ---- ordering
    table: Optional[int]        # sticky: set by navigation to "Table N" or asked once
    order: list[dict]           # committed lines {name, qty, variation, kind, price, menu_id}
    pending: list[dict]         # lines still missing name/qty/variation
    suggest_line: Optional[dict]
    # ---- menu
    menu_category: Optional[str]
    menu_focus: Optional[str]   # item currently being described (for "want to order it?")


# What each `awaiting` token belongs to, so a bare "yes"/"two"/"table 3" reaches the right node.
AWAIT_OWNER = {
    "nav_location": "navigation", "nav_confirm": "navigation",
    "order_table": "ordering", "order_item": "ordering", "order_qty": "ordering",
    "order_variation": "ordering", "order_more": "ordering", "order_confirm": "ordering",
    "order_suggest": "ordering", "order_prompt": "ordering",
    "menu_category": "menu", "menu_item": "menu", "menu_order": "menu", "menu_more": "menu",
}
