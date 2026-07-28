"""
conversation.py
---------------
Manages conversation state:
  - Full message history (for Ollama multi-turn context)
  - Active scenario detection and tracking
  - Last TTS utterance (for REPEAT intent)
  - Slot memory (last known place, room, item, etc.)
"""

from dataclasses import dataclass, field
from typing import Optional

from order_cart import OrderCart


@dataclass
class ConversationContext:
    history: list = field(default_factory=list)        # [{role, content}, ...]
    active_scenario: Optional[str] = None              # detected scenario
    last_bot_utterance: str = ""                       # for REPEAT intent
    last_intent: Optional[str] = None
    slots: dict = field(default_factory=dict)          # {place, room, item, temperature...}
    turn_count: int = 0

    # ── Order-taking (restaurant) ────────────────────────────────────────
    table_id: Optional[str] = None    # set by the test harness / real arrival trigger
    map_name: Optional[str] = None    # which waypoints/orders file this table belongs to
    taking_order: bool = False        # True while actively collecting items for this table
    awaiting_confirmation: bool = False  # True once the customer's said "done" and heard
                                          # the order read back — next turn either confirms
                                          # (submits) or, if it's something else, cancels back
                                          # to normal item editing (see main.py's _handle_order_turn)
    cart: OrderCart = field(default_factory=OrderCart)

    # ── History management ──────────────────────────────────────────────
    def add_user(self, text: str):
        self.history.append({"role": "user", "content": text})
        self.turn_count += 1

    def add_assistant(self, text: str):
        self.history.append({"role": "assistant", "content": text})
        self.last_bot_utterance = text

    def trimmed_history(self, max_turns: int = 10) -> list:
        """Return last N turns to keep context window small."""
        return self.history[-(max_turns * 2):]

    # ── Scenario tracking ───────────────────────────────────────────────
    def set_scenario(self, scenario: str):
        if scenario and scenario != self.active_scenario:
            print(f"  [Context] Scenario switched: {self.active_scenario} → {scenario}")
            self.active_scenario = scenario

    # ── Slot tracking ───────────────────────────────────────────────────
    def update_slots(self, new_slots: dict):
        for k, v in new_slots.items():
            if v:
                self.slots[k] = v

    def get_slot(self, key: str, default: str = "unknown") -> str:
        return self.slots.get(key, default)

    # ── Debug ────────────────────────────────────────────────────────────
    def summary(self) -> str:
        return (
            f"Turn #{self.turn_count} | "
            f"Scenario: {self.active_scenario or 'none'} | "
            f"Last intent: {self.last_intent or 'none'} | "
            f"Slots: {self.slots}"
        )

    def reset(self):
        """Clear all conversation state — keeps scenario intact."""
        self.history.clear()
        self.last_bot_utterance = ""
        self.last_intent = None
        self.slots = {}
        self.turn_count = 0
