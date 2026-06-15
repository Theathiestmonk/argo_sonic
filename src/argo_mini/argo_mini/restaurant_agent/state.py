"""
Shared LangGraph state for the restaurant waiter robot agent.

All nodes and subgraphs read from and write to this TypedDict.
"""

from typing import TypedDict, List, Optional, Dict, Any


class OrderItem(TypedDict):
    menu_id: str
    name: str
    quantity: int
    unit_price: float
    notes: str


class AgentState(TypedDict):
    # ── Audio I/O ─────────────────────────────────────────────────────────────
    user_input: str          # Vosk-recognized text (set each turn)
    response: str            # Robot's spoken response (set each turn)

    # ── Session (persists for entire table visit) ─────────────────────────────
    session_id: str
    table_id: str
    customer_name: str
    phone_number: str
    language: str            # english / hindi / gujarati

    # ── Conversation (accumulates across turns) ───────────────────────────────
    conversation_history: List[Dict[str, str]]

    # ── Intent (resolved each turn) ───────────────────────────────────────────
    current_intent: str
    confidence: float
    entities: Dict[str, Any]

    # ── Multi-turn flow control ───────────────────────────────────────────────
    # next_action tells the router what the robot is waiting for across turns.
    # When set, it bypasses intent classification and routes directly.
    next_action: str
    # Values: "" | "await_name" | "await_phone" | "await_items"
    #         "await_confirm_order" | "await_confirm_suggestion"
    #         "await_more_items" | "await_feedback"
    active_subgraph: str     # "" | "order" | "billing" | "navigation" | "greeting"

    # ── Order ─────────────────────────────────────────────────────────────────
    current_order: List[OrderItem]
    pending_raw_text: str    # raw user text for item parsing
    pending_suggestions: List[Dict]  # fuzzy-match candidates awaiting confirmation
    order_id: str
    order_status: str        # draft / confirmed / sent_to_kitchen / ready / delivered

    # ── Navigation ────────────────────────────────────────────────────────────
    destination: str         # "table_3" | "kitchen" | "home"
    robot_location: str
    navigation_status: str   # idle / navigating / arrived / failed

    # ── Kitchen ───────────────────────────────────────────────────────────────
    kitchen_status: str      # waiting / preparing / ready

    # ── Billing ───────────────────────────────────────────────────────────────
    bill_generated: bool
    bill_amount: float
    payment_status: str      # unpaid / paid

    # ── Emergency ─────────────────────────────────────────────────────────────
    emergency_type: str      # none / collision / low_battery / help_requested

    # ── Task ──────────────────────────────────────────────────────────────────
    task_status: str         # idle / active / completed / failed
    error_message: str


def default_state(table_id: str = "", session_id: str = "") -> AgentState:
    """Return a fresh session state."""
    import uuid
    return AgentState(
        user_input="",
        response="",
        session_id=session_id or str(uuid.uuid4())[:8],
        table_id=table_id,
        customer_name="",
        phone_number="",
        language="english",
        conversation_history=[],
        current_intent="",
        confidence=0.0,
        entities={},
        next_action="",
        active_subgraph="",
        current_order=[],
        pending_raw_text="",
        pending_suggestions=[],
        order_id="",
        order_status="draft",
        destination="",
        robot_location="home",
        navigation_status="idle",
        kitchen_status="waiting",
        bill_generated=False,
        bill_amount=0.0,
        payment_status="unpaid",
        emergency_type="none",
        task_status="idle",
        error_message="",
    )
