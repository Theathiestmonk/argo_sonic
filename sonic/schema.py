"""
Data schema for the Sonic robot NLP pipeline.

This defines the shared state object that flows through every node of the
LangGraph graph (graph.py). Every node reads from and writes to this same
state dict, so it doubles as the contract between wake-word detection,
intent classification, and every branch handler (menu, take order,
navigation, cutlery, about, etc.)
"""

from typing import TypedDict, Optional, List, Literal
from typing_extensions import NotRequired


# ---------------------------------------------------------------------------
# Leaf-level types
# ---------------------------------------------------------------------------

class OrderItem(TypedDict):
    """A single line item inside an order."""
    item_id: str
    name: str
    category: Literal[
        "starters",
        "soups",
        "salads",
        "snacks",
        "main_course",
        "breads",
        "desserts",
        "drinks",
        "beverages",
        "specials",
    ]
    quantity: int
    preference: NotRequired[Optional[str]]   # e.g. "no onion", "extra spicy"


class ConversationTurn(TypedDict):
    """One turn of dialogue, kept for context across multi-step flows."""
    role: Literal["user", "robot"]
    text: str


# ---------------------------------------------------------------------------
# Top-level intents (row 2 of the flowchart, children of Intent Classify)
# ---------------------------------------------------------------------------

IntentType = Literal[
    "menu",
    "call_people",
    "take_order",
    "navigation",
    "cutlery",
    "about",
    "normal_conv",
    "get_bill",
]

MenuCategory = Literal[
    "starters",
    "soups",
    "salads",
    "snacks",
    "main_course",
    "breads",
    "desserts",
    "drinks",
    "beverages",
    "specials",
]
CallTarget = Literal["manager", "waiter", "cleaner"]
CutleryItem = Literal["spoon", "fork", "glass"]
AboutTopic = Literal["who_are_you", "what_can_you_do", "about_robot", "famous_for", "other"]
NavLocation = Literal["docker", "kitchen", "table"]

OrderAction = Literal["new_order", "edit_order", "cancel_order", "add_item"]
EditAction = Literal["replace", "customize", "cancel_single", "cancel_full"]

# Which slot the order sub-flow is currently trying to fill
OrderSlot = Literal[
    "ask_item", "ask_qty", "ask_preference", "ask_to_add", "confirm_order", None
]


# ---------------------------------------------------------------------------
# Root state
# ---------------------------------------------------------------------------

class NLPState(TypedDict):
    # --- input / wake word ---------------------------------------------------
    raw_transcript: str
    wake_word_detected: bool

    # --- intent classification (LLM) -----------------------------------------
    intent: Optional[IntentType]
    confidence: NotRequired[Optional[float]]

    # --- shared dialogue context ----------------------------------------------
    conversation_history: List[ConversationTurn]
    table_no: NotRequired[Optional[str]]

    # --- branch: menu ----------------------------------------------------------
    menu_category: NotRequired[Optional[MenuCategory]]

    # --- branch: call_people -----------------------------------------------
    call_target: NotRequired[Optional[CallTarget]]

    # --- branch: take_order (the deepest sub-flow) ---------------------------
    order_action: NotRequired[Optional[OrderAction]]
    edit_action: NotRequired[Optional[EditAction]]
    current_order: NotRequired[List[OrderItem]]
    active_item: NotRequired[Optional[OrderItem]]      # item currently being built/edited
    order_slot: NotRequired[OrderSlot]                 # which question we're currently asking
    awaiting_yes_no: NotRequired[Optional[Literal["add_more", "confirm_order"]]]
    order_confirmed: NotRequired[bool]

    # --- branch: navigation ---------------------------------------------------
    location: NotRequired[Optional[NavLocation]]

    # --- branch: cutlery --------------------------------------------------------
    cutlery_requested: NotRequired[List[CutleryItem]]

    # --- branch: about ----------------------------------------------------------
    about_topic: NotRequired[Optional[AboutTopic]]

    # --- branch: get_bill ---------------------------------------------------
    bill_total: NotRequired[Optional[float]]

    # --- output / dispatch to robot hardware -----------------------------------
    robot_action: NotRequired[Optional[dict]]   # e.g. {"type": "navigate", "target": "kitchen"}
    response_text: NotRequired[Optional[str]]
    error: NotRequired[Optional[str]]
