"""
Intent classification node.

Uses Ollama (qwen2.5:1.5b) to extract intent + entities from Vosk-recognized text.
Fast-path: checks next_action first — if robot is mid-conversation, skips LLM.
"""

import json
import logging
import re
import requests
from typing import Dict, Any

from ..state import AgentState

logger = logging.getLogger("argo_intent")

OLLAMA_URL = "http://localhost:11434/api/generate"
MODEL = "qwen2.5:1.5b"

INTENT_PROMPT = """\
You are an intent classifier for a restaurant robot waiter.
Classify the input into one intent and extract entities.

Intents:
- greeting        : hello, hi, good morning/afternoon/evening
- place_order     : I want, give me, order food
- modify_order    : add, remove, change quantity
- cancel_order    : cancel my order
- request_bill    : bill, check, pay, payment
- navigation_request : go to table N, deliver to table N
- return_home     : go home, return to base, dock
- kitchen_request : go to kitchen, pick up order
- general_chat    : jokes, small talk, questions about robot
- help            : need help, assistance
- emergency       : stop, danger, emergency

Respond ONLY with JSON:
{"intent":"<intent>","confidence":<0-1>,"entities":{"table":<int or null>,"items":[<str>],"quantity":<int or null>}}

Examples:
"go to table 5"            → {"intent":"navigation_request","confidence":0.98,"entities":{"table":5,"items":[],"quantity":null}}
"I want paneer pizza"      → {"intent":"place_order","confidence":0.95,"entities":{"table":null,"items":["paneer pizza"],"quantity":1}}
"two garlic bread please"  → {"intent":"place_order","confidence":0.95,"entities":{"table":null,"items":["garlic bread"],"quantity":2}}
"bring the bill"           → {"intent":"request_bill","confidence":0.97,"entities":{"table":null,"items":[],"quantity":null}}
"tell me a joke"           → {"intent":"general_chat","confidence":0.90,"entities":{"table":null,"items":[],"quantity":null}}

Input: """

# ── Number word → int ─────────────────────────────────────────────────────────
_NUM_MAP = {
    "one":1,"two":2,"three":3,"four":4,"five":5,"six":6,
    "seven":7,"eight":8,"nine":9,"ten":10,"eleven":11,"twelve":12,
}

def _extract_table(text: str) -> int | None:
    m = re.search(r'table\s+(?:number\s+)?(\d+|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)', text.lower())
    if m:
        v = m.group(1)
        return _NUM_MAP.get(v, int(v) if v.isdigit() else None)
    return None


def intent_classifier_node(state: AgentState) -> dict:
    """
    LangGraph node: classify intent from user_input.

    If the robot is mid-conversation (next_action set), skip LLM and
    route directly — the subgraph will interpret the text in context.
    """
    text = state.get("user_input", "").strip()
    next_action = state.get("next_action", "")

    # ── Fast path: mid-conversation, don't re-classify ────────────────────────
    if next_action and next_action.startswith("await_"):
        logger.debug(f"[INTENT] Mid-conversation ({next_action}), skipping LLM")
        return {}   # state unchanged, router uses next_action

    # ── Fast path: emergency keywords ─────────────────────────────────────────
    if any(w in text.lower() for w in ["emergency", "stop", "danger", "help me", "call someone"]):
        return {"current_intent": "emergency", "confidence": 0.99, "entities": {}}

    # ── LLM classification ────────────────────────────────────────────────────
    try:
        resp = requests.post(
            OLLAMA_URL,
            json={"model": MODEL, "prompt": INTENT_PROMPT + text, "stream": False, "temperature": 0.1},
            timeout=8,
        )
        resp.raise_for_status()
        raw = resp.json().get("response", "").strip()

        m = re.search(r'\{.*\}', raw, re.DOTALL)
        if m:
            parsed = json.loads(m.group())
            intent = parsed.get("intent", "general_chat")
            confidence = float(parsed.get("confidence", 0.7))
            entities = parsed.get("entities", {})

            # Override table entity with regex if LLM missed it
            if intent == "navigation_request" and not entities.get("table"):
                entities["table"] = _extract_table(text)

            logger.info(f"[INTENT] '{text}' → {intent} ({confidence:.2f}) entities={entities}")
            return {"current_intent": intent, "confidence": confidence, "entities": entities}

    except Exception as e:
        logger.warning(f"[INTENT] LLM failed ({e}), using fallback")

    # ── Regex fallback ─────────────────────────────────────────────────────────
    tl = text.lower()
    if re.search(r'\btable\s+\w+', tl):
        return {"current_intent": "navigation_request", "confidence": 0.85,
                "entities": {"table": _extract_table(text)}}
    if any(w in tl for w in ["order", "want", "give me", "i'll have"]):
        return {"current_intent": "place_order", "confidence": 0.75, "entities": {}}
    if any(w in tl for w in ["bill", "check", "pay"]):
        return {"current_intent": "request_bill", "confidence": 0.85, "entities": {}}
    if any(w in tl for w in ["hello", "hi ", "good morning", "good afternoon", "good evening"]):
        return {"current_intent": "greeting", "confidence": 0.90, "entities": {}}
    if any(w in tl for w in ["home", "base", "dock", "return"]):
        return {"current_intent": "return_home", "confidence": 0.85, "entities": {}}

    return {"current_intent": "general_chat", "confidence": 0.5, "entities": {}}
