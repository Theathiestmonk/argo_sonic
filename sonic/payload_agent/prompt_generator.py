"""Turns a get_next_question() key (or an execute-time announcement) into
spoken text via the LLM — same warm-waiter-persona render() pattern
main_agent.py uses, so phrasing varies naturally instead of scripted lines
on repeat.
"""

from __future__ import annotations

from typing import Optional

from llm_client import LLMClient
from models import Payload

_WRAPPING_QUOTES = "\"'“”‘’"

WAITER_PERSONA = (
    "You are Sonic, a warm and genuinely friendly restaurant service robot taking a table's food order "
    "in person. You are female — if it ever comes up, use she/her, never he/him. Sound like an attentive "
    "human waiter, not a script — vary your phrasing naturally rather than repeating the same sentence "
    "structure every time. Speak natural Indian English — the guest is in India, so phrase things the way "
    "an Indian waiter would, not American/British English. Keep replies to 1-2 short spoken sentences. "
    "Never wrap your reply in quotation marks — plain spoken text only. Never invent menu items, prices, "
    "or facts that aren't given to you in the context below."
)

QUESTION_HINTS = {
    "which_table": "Ask which table you should come to.",
    "item_name": "Ask what dish they'd like to order.",
    "item_qty": "Ask how many of {item_name} they'd like.",
    "anything_else": "Their order so far: {order_summary}. Ask if there's anything else they'd like.",
    "confirm_order": "Read back their order for confirmation: {order_summary}, total {total}. Ask if that's correct.",
    "menu_category": "Ask which menu category they'd like to hear about. Available: {categories}.",
    "menu_next_step": "Ask if they'd like to order something, hear about a different category, or if "
                       "they're done browsing the menu.",
}

ANNOUNCEMENT_HINTS = {
    "wake_ack": "They just said your wake word and you're now listening — give ONLY a very short "
                "acknowledgment that you heard them and are ready, 1-4 words, e.g. \"Hi!\", \"Hello!\", "
                "\"Yes?\", \"I'm here!\". NOT a full sentence, and don't ask what they'd like — they'll "
                "just say it.",
    "arrived_at_table": "You've just arrived at Table {table}. Let them know briefly.",
    "menu_description": "Describe the {category} menu category. Items: {items}.",
    "order_confirmed": "Their order is confirmed and headed to the kitchen: {order_summary}, total {total}. "
                        "Thank them warmly and let them know it's on its way.",
    "about_cafe": "The guest asked about the cafe. Tell them: {about_text}",
    "get_bill": "Their bill for this visit: {order_summary}, total {total}. Read it out.",
    "farewell": "Say a brief, warm goodbye.",
    "nav_failed": "You weren't able to reach the table due to a navigation problem. Apologize briefly and "
                  "let them know staff will help.",
}


def clean_spoken_text(text: Optional[str]) -> str:
    if not text:
        return ""
    cleaned = text.strip().strip(_WRAPPING_QUOTES).strip()
    if cleaned and cleaned[0].isalpha():
        cleaned = cleaned[0].upper() + cleaned[1:]
    return cleaned


def _build_prompt(hint_map: dict, key: str, payload: Payload, **ctx) -> str:
    hint = hint_map.get(key, key)
    try:
        hint = hint.format(**ctx)
    except (KeyError, IndexError):
        pass
    history_tail = payload.conversation_history[-6:]
    history_text = "\n".join(f"{t['role']}: {t['text']}" for t in history_tail) or "(nothing yet)"
    return f"Recent conversation:\n{history_text}\n\nWhat to say now: {hint}"


def render_question(llm: LLMClient, key: str, payload: Payload, **ctx) -> str:
    """key is one of get_next_question()'s return values (e.g. "item_qty")."""
    user_prompt = _build_prompt(QUESTION_HINTS, key, payload, **ctx)
    try:
        text = llm.chat(WAITER_PERSONA, user_prompt)
    except Exception as e:
        print(f"[warn] render_question failed for key={key!r}: {e}")
        return "Sorry, I'm having a little trouble right now — one moment."
    return clean_spoken_text(text) or "Sorry, one moment."


def render_announcement(llm: LLMClient, key: str, payload: Payload, **ctx) -> str:
    """key is one of ANNOUNCEMENT_HINTS (execute-time / fire-and-forget lines)."""
    user_prompt = _build_prompt(ANNOUNCEMENT_HINTS, key, payload, **ctx)
    try:
        text = llm.chat(WAITER_PERSONA, user_prompt)
    except Exception as e:
        print(f"[warn] render_announcement failed for key={key!r}: {e}")
        return "Sorry, I'm having a little trouble right now — one moment."
    return clean_spoken_text(text) or "Sorry, one moment."
