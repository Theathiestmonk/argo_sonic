"""
Order taking subgraph.

Multi-turn flow tracked via next_action:
  "" / "start"       → ask name (if missing)
  "await_name"       → validate name → ask phone
  "await_phone"      → validate phone → take order
  "await_items"      → parse & validate items → confirm or suggest
  "await_confirm_suggestion" → accept/reject fuzzy match
  "await_confirm_order"      → yes/no → send to POS
  "await_more_items" → add more or finish

LLM is used to extract items + quantities from natural language.
"""

import json
import logging
import re
import requests
from langgraph.graph import StateGraph, START, END
from ..state import AgentState, OrderItem
from ..integrations.menu import MenuDatabase
from ..integrations.pos import POSClient

logger = logging.getLogger("argo_ordering")
OLLAMA_URL = "http://localhost:11434/api/generate"
MODEL = "qwen2.5:1.5b"

menu_db = MenuDatabase()
pos_client = POSClient()

# ── Item parser ───────────────────────────────────────────────────────────────

PARSE_PROMPT = """\
Extract food items and quantities from this restaurant order.
Respond ONLY with JSON array: [{"item":"<name>","qty":<int>}]
Examples:
"I want two paneer pizza and one garlic bread" → [{"item":"paneer pizza","qty":2},{"item":"garlic bread","qty":1}]
"give me a lassi" → [{"item":"lassi","qty":1}]
"three butter naan" → [{"item":"butter naan","qty":3}]
Input: """


def _parse_items_llm(text: str) -> list:
    try:
        resp = requests.post(
            OLLAMA_URL,
            json={"model": MODEL, "prompt": PARSE_PROMPT + text, "stream": False, "temperature": 0.1},
            timeout=8,
        )
        resp.raise_for_status()
        raw = resp.json().get("response", "")
        m = re.search(r'\[.*\]', raw, re.DOTALL)
        if m:
            return json.loads(m.group())
    except Exception as e:
        logger.warning(f"[ORDER] Item parse LLM failed: {e}")
    return []


def _yes(text: str) -> bool:
    return any(w in text.lower() for w in ["yes", "yeah", "yep", "sure", "ok", "correct", "confirm", "right"])


def _no(text: str) -> bool:
    return any(w in text.lower() for w in ["no", "nope", "don't", "cancel", "wrong", "change"])


# ── Nodes ─────────────────────────────────────────────────────────────────────

def order_flow_node(state: AgentState) -> dict:
    """
    Single node handling all order states.
    Reads next_action to know where in the flow we are.
    """
    text = state.get("user_input", "").strip()
    next_action = state.get("next_action", "")

    # ── Step 1: Get customer name ─────────────────────────────────────────────
    if not state.get("customer_name") or next_action == "await_name":
        if next_action == "await_name":
            name = text.strip().title()
            if len(name) >= 2 and not any(c.isdigit() for c in name):
                if not state.get("phone_number"):
                    return {
                        "customer_name": name,
                        "response": f"Nice to meet you, {name}! May I have your 10-digit phone number?",
                        "next_action": "await_phone",
                        "active_subgraph": "order",
                    }
                return {
                    "customer_name": name,
                    "response": f"Great, {name}! What would you like to order today?",
                    "next_action": "await_items",
                    "active_subgraph": "order",
                }
            return {
                "response": "I didn't catch your name. Could you tell me your name?",
                "next_action": "await_name",
                "active_subgraph": "order",
            }
        return {
            "response": "I'd love to take your order! May I have your name first?",
            "next_action": "await_name",
            "active_subgraph": "order",
        }

    # ── Step 2: Get phone number ──────────────────────────────────────────────
    if not state.get("phone_number") or next_action == "await_phone":
        if next_action == "await_phone":
            phone = re.sub(r'\D', '', text)
            if len(phone) == 10:
                return {
                    "phone_number": phone,
                    "response": f"Perfect! What would you like to order, {state['customer_name']}?",
                    "next_action": "await_items",
                    "active_subgraph": "order",
                }
            return {
                "response": "Please share your 10-digit phone number.",
                "next_action": "await_phone",
                "active_subgraph": "order",
            }
        return {
            "response": "Could I have your phone number for the order record?",
            "next_action": "await_phone",
            "active_subgraph": "order",
        }

    # ── Step 3: Accept fuzzy-match confirmation ───────────────────────────────
    if next_action == "await_confirm_suggestion":
        suggestions = state.get("pending_suggestions", [])
        if _yes(text) and suggestions:
            item = suggestions[0]
            existing = list(state.get("current_order", []))
            # Check if already in order
            found = next((o for o in existing if o["menu_id"] == item["id"]), None)
            if found:
                found["quantity"] += 1
            else:
                existing.append(OrderItem(
                    menu_id=item["id"], name=item["name"],
                    quantity=1, unit_price=item["price"], notes="",
                ))
            return {
                "current_order": existing,
                "pending_suggestions": [],
                "response": f"Added {item['name']} to your order. Anything else?",
                "next_action": "await_more_items",
                "active_subgraph": "order",
            }
        return {
            "pending_suggestions": [],
            "response": "No problem! What else would you like?",
            "next_action": "await_items",
            "active_subgraph": "order",
        }

    # ── Step 4: Take items ────────────────────────────────────────────────────
    if next_action in ("await_items", "await_more_items", ""):
        parsed = _parse_items_llm(text)
        if not parsed:
            return {
                "response": "Sorry, I didn't catch that. What would you like to order?",
                "next_action": "await_items",
                "active_subgraph": "order",
            }

        existing = list(state.get("current_order", []))
        not_found = []
        needs_confirm = []

        for entry in parsed:
            item_name = entry.get("item", "")
            qty = max(1, int(entry.get("qty", 1)))
            result = menu_db.find_item(item_name)

            if result["exact"]:
                item = result["exact"]
                found = next((o for o in existing if o["menu_id"] == item["id"]), None)
                if found:
                    found["quantity"] += qty
                else:
                    existing.append(OrderItem(
                        menu_id=item["id"], name=item["name"],
                        quantity=qty, unit_price=item["price"], notes="",
                    ))
            elif result["fuzzy"]:
                needs_confirm.append({"query": item_name, "match": result["fuzzy"], "qty": qty})
            else:
                not_found.append(item_name)

        # Handle fuzzy match: confirm one at a time
        if needs_confirm:
            candidate = needs_confirm[0]
            return {
                "current_order": existing,
                "pending_suggestions": [candidate["match"]],
                "response": f"Did you mean {candidate['match']['name']} at ₹{candidate['match']['price']}?",
                "next_action": "await_confirm_suggestion",
                "active_subgraph": "order",
            }

        if not_found:
            parts = ", ".join(not_found)
            return {
                "current_order": existing,
                "response": f"Sorry, we don't have {parts}. Would you like something else?",
                "next_action": "await_items",
                "active_subgraph": "order",
            }

        if existing:
            summary = menu_db.format_order_summary(existing)
            return {
                "current_order": existing,
                "response": f"Got it! Your order so far: {summary}. Shall I confirm this order?",
                "next_action": "await_confirm_order",
                "active_subgraph": "order",
            }

        return {
            "response": "What would you like to order?",
            "next_action": "await_items",
            "active_subgraph": "order",
        }

    # ── Step 5: Confirm and send to POS ──────────────────────────────────────
    if next_action == "await_confirm_order":
        if _yes(text):
            order = state.get("current_order", [])
            order_id = pos_client.create_order(
                table_id=state.get("table_id", ""),
                customer_name=state.get("customer_name", ""),
                phone=state.get("phone_number", ""),
                items=order,
                session_id=state.get("session_id", ""),
            )
            if order_id:
                # Upsell suggestion
                categories = {i["menu_id"][0] for i in order}
                upsell = ""
                if "M" in categories and not any(i["menu_id"].startswith("B") for i in order):
                    upsell = " Would you also like some Garlic Naan or Butter Naan with your meal?"
                if "M" in categories and not any(i["menu_id"].startswith("DR") for i in order):
                    upsell = upsell or " Can I get you a Lassi or Masala Chai?"

                return {
                    "order_id": order_id,
                    "order_status": "confirmed",
                    "response": (f"Order confirmed! Your order number is {order_id}. "
                                 f"I'll send it to the kitchen now.{upsell}"),
                    "next_action": "",
                    "active_subgraph": "",
                    "task_status": "completed",
                }
            return {
                "response": "Sorry, there was an issue sending your order. Please try again.",
                "next_action": "await_confirm_order",
                "active_subgraph": "order",
                "task_status": "failed",
            }

        if _no(text):
            return {
                "response": "No problem! What changes would you like to make?",
                "next_action": "await_items",
                "active_subgraph": "order",
            }

        return {
            "response": "Please say yes to confirm or no to make changes.",
            "next_action": "await_confirm_order",
            "active_subgraph": "order",
        }

    # ── Step 6: Ask for more items ────────────────────────────────────────────
    if next_action == "await_more_items":
        if _no(text) or any(w in text.lower() for w in ["that's all", "nothing", "done", "finish"]):
            order = state.get("current_order", [])
            if order:
                summary = menu_db.format_order_summary(order)
                return {
                    "response": f"Your order: {summary}. Shall I confirm?",
                    "next_action": "await_confirm_order",
                    "active_subgraph": "order",
                }
            return {
                "response": "No order placed. Let me know if you'd like anything!",
                "next_action": "",
                "active_subgraph": "",
            }
        # Treat as more items
        return order_flow_node({**state, "next_action": "await_items"})

    return {
        "response": "How can I help with your order?",
        "next_action": "await_items",
        "active_subgraph": "order",
    }


def build_ordering_subgraph() -> StateGraph:
    sg = StateGraph(AgentState)
    sg.add_node("order_flow", order_flow_node)
    sg.add_edge(START, "order_flow")
    sg.add_edge("order_flow", END)
    return sg.compile()
