"""Pydantic payload schema for the payload-driven Sonic agent.

Replaces the ~20-node LangGraph state machine with a single Payload object
that an extraction step fills in turn by turn. A payload is "complete" for
its intent once every field that intent needs is non-null; until then,
get_next_question() says what to ask next. Modifications are always
opportunistic (captured only if the guest mentions them in the same breath
as the item) — they never block an item from being considered complete.
"""

from __future__ import annotations

from enum import Enum
from typing import Dict, List, Optional

from pydantic import BaseModel, Field

KITCHEN_LOCATION = 0


class Intent(str, Enum):
    NONE = "none"
    TAKE_ORDER = "take_order"
    TELL_MENU = "tell_menu"
    NAVIGATE = "navigate"
    DELIVER_ORDER = "deliver_order"
    GET_BILL = "get_bill"
    ABOUT_CAFE = "about_cafe"
    NORMAL_CONV = "normal_conv"


# Intents that need order_table filled before anything else can happen.
TABLE_REQUIRED_INTENTS = {
    Intent.TAKE_ORDER,
    Intent.TELL_MENU,
    Intent.NAVIGATE,
    Intent.DELIVER_ORDER,
    Intent.GET_BILL,
}

# Intents where the robot must physically be at order_table before the
# intent's own fields matter (menu can be recited from anywhere the guest
# is asking from, since the "table" there is just where to send the order —
# so it is NOT in this set; take_order/deliver/get_bill genuinely need the
# robot present).
NAV_REQUIRED_INTENTS = {
    Intent.TAKE_ORDER,
    Intent.NAVIGATE,
    Intent.DELIVER_ORDER,
    Intent.GET_BILL,
}


class OrderItem(BaseModel):
    serial_no: int
    name: Optional[str] = None
    qty: Optional[int] = None
    modifications: Dict[str, str] = Field(default_factory=dict)

    def is_complete(self) -> bool:
        return bool(self.name) and self.qty is not None and self.qty > 0


class Payload(BaseModel):
    intent: Intent = Intent.NONE
    order_table: Optional[int] = None
    robot_location: Optional[int] = None  # 0 = kitchen, N = table N
    order_items: Dict[int, OrderItem] = Field(default_factory=dict)
    current_menu_category: Optional[str] = None
    order_total: float = 0.0

    # transient turn-answers — reset to None once consumed
    wants_more: Optional[bool] = None  # answer to "anything else?"
    confirmed: Optional[bool] = None  # answer to final order readback
    menu_category_described: bool = False  # has current_menu_category's items been spoken yet
    menu_done: Optional[bool] = None  # answer to "another category, order, or done browsing?"

    conversation_history: List[Dict[str, str]] = Field(default_factory=list)

    # ---- read-only queries -------------------------------------------------

    def _last_item(self) -> Optional[OrderItem]:
        if not self.order_items:
            return None
        return self.order_items[max(self.order_items)]

    def is_complete_for_intent(self) -> bool:
        if self.intent == Intent.NONE:
            return False

        if self.intent in TABLE_REQUIRED_INTENTS and self.order_table is None:
            return False

        if self.intent in NAV_REQUIRED_INTENTS and self.robot_location != self.order_table:
            return False

        if self.intent == Intent.TAKE_ORDER:
            if not self.order_items:
                return False
            if not all(item.is_complete() for item in self.order_items.values()):
                return False
            if self.wants_more is None:
                return False
            if self.wants_more:
                return False  # a fresh item slot should have been opened
            if self.confirmed is None:
                return False
            return self.confirmed is True

        if self.intent == Intent.TELL_MENU:
            return self.menu_done is True

        # NAVIGATE / DELIVER_ORDER / GET_BILL / ABOUT_CAFE / NORMAL_CONV:
        # table+nav gates above (if any) already covered everything they need.
        return True

    def get_next_question(self) -> Optional[str]:
        """What to ask/do next, or None if the payload is execute-ready.

        "navigate" is a special sentinel — the caller triggers real
        navigation instead of speaking a question."""
        if self.intent == Intent.NONE:
            return None  # caller must classify first; nothing to ask yet

        if self.intent in TABLE_REQUIRED_INTENTS and self.order_table is None:
            return "which_table"

        if self.intent in NAV_REQUIRED_INTENTS and self.robot_location != self.order_table:
            return "navigate"

        if self.intent == Intent.TAKE_ORDER:
            last = self._last_item()
            if last is None or not last.name:
                return "item_name"
            if last.qty is None:
                return "item_qty"
            if self.wants_more is None:
                return "anything_else"
            if self.wants_more:
                return "item_name"  # PayloadManager opens a fresh slot before this fires
            if not self.confirmed:
                # Covers both None (never asked) and False (extraction set it
                # alongside a correction instead of leaving it null, despite
                # the prompt saying not to — live-observed with gpt-4o-mini).
                # Either way, the only way out of this stage is a real yes,
                # so keep re-asking rather than falling through to None
                # while is_complete_for_intent() still says not-ready —
                # that mismatch would silently strand the caller.
                return "confirm_order"
            return None

        if self.intent == Intent.TELL_MENU:
            if self.current_menu_category is None:
                return "menu_category"
            if not self.menu_category_described:
                return "describe_category"  # announcement, not a question — see main loop
            if not self.menu_done:
                return "menu_next_step"
            return None

        return None

    def order_summary(self) -> str:
        if not self.order_items:
            return "no items yet"
        parts = []
        for item in self.order_items.values():
            if not item.name:
                continue
            mods = f" ({', '.join(f'{k}: {v}' for k, v in item.modifications.items())})" if item.modifications else ""
            parts.append(f"{item.qty or '?'}x {item.name}{mods}")
        return ", ".join(parts) if parts else "no items yet"
