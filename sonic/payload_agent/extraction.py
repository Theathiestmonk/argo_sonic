"""LLM-based extraction: turns one guest utterance + the current Payload
into a raw JSON dict shaped for payload_manager.merge()/resolve_item_changes().

Always re-extracts intent (per-turn intent recheck, as decided) — the
model sees the current payload state and can signal a topic switch at any
point; payload_manager.should_clear_history() decides what that means for
conversation history.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Optional

from llm_client import LLMClient
from models import Intent, Payload

EXTRACTION_SCHEMA = """{
  "intent": "<take_order, tell_menu, navigate, deliver_order, get_bill, about_cafe, normal_conv, or null>",
  "order_table": <the table number mentioned, as an integer, or null>,
  "current_menu_category": "<menu category mentioned, as free text, or null>",
  "wants_more": <true/false/null — only for a yes/no answer to \\"anything else?\\">,
  "confirmed": <true/false/null — only for a yes/no answer confirming the final order readback>,
  "menu_done": <true/false/null — only for a yes/no answer to \\"anything else from the menu, or done browsing?\\">,
  "item_changes": [
    {"name": "<dish name>", "qty": <integer or null>, "modifications": {"<option>": "<value>"}}
  ],
  "response_text": "<short warm natural reply, ONLY for genuine unrelated small talk, else empty string>"
}"""

EXPECTS_GUIDANCE = {
    "intent_and_table": "This is the opening turn — figure out what they want and set intent. IMPORTANT: if "
                         "they name ANY dish/item at all (\"can I get a coffee\", \"two pizzas please\"), "
                         "that itself IS a take_order request even if they never say the word \"order\" — "
                         "always set intent=take_order whenever item_changes ends up non-empty. Pull out a "
                         "table number into order_table if mentioned in the same breath (e.g. \"table 4, "
                         "I'll have a coffee\").",
    "which_table": "They should be telling you a table number. Extract it into order_table.",
    "item_name": "They're telling you what dish(es) they want. Extract every distinct dish mentioned into "
                 "item_changes, each with qty/modifications if they mentioned those too.",
    "item_qty": "They should be telling you how many of the current item. Extract it as an integer — you "
                "don't need to repeat the dish name, just put the qty in item_changes with no name.",
    "anything_else": "This is an \"anything else?\" question. If they name a new dish, put it in "
                      "item_changes (that counts as answering yes). Otherwise map their reply to wants_more.",
    "confirm_order": "You just read back their order for confirmation. If they confirm, set confirmed=true. "
                      "If they want to change something (e.g. reduce a quantity, remove an item), extract "
                      "that change into item_changes and leave confirmed completely null/absent — do NOT "
                      "set confirmed=false for a correction, only for an explicit outright rejection of the "
                      "whole order with no edit given. A correction is not a rejection, just an edit to "
                      "apply; you'll read the updated order back and ask again next turn.",
    "menu_category": "They're telling you which menu category they want to hear about, or naming a specific "
                      "dish directly. Extract the category into current_menu_category, or the dish into "
                      "item_changes if they named one instead.",
    "menu_next_step": "You just described a menu category. If they want to order something, set "
                       "intent=take_order and put the dish in item_changes. If they want to hear about a "
                       "different category, extract it into current_menu_category. Otherwise map their "
                       "yes/no reply (done browsing vs. not) to menu_done.",
}


def build_system_prompt(payload: Payload, expects: str) -> str:
    return f"""You are the understanding module for "Sonic", a restaurant robot taking a table's order by \
voice. Read the guest's latest utterance and return ONLY a JSON object (no prose, no markdown fences) with \
exactly this shape:

{EXTRACTION_SCHEMA}

Current stage: {expects} — {EXPECTS_GUIDANCE.get(expects, "")}
Current payload: intent={payload.intent.value}, order_table={payload.order_table}, \
order_items={payload.order_summary()}

Rules:
- Only include fields the guest's utterance actually addresses — leave everything else null/empty. Do NOT \
guess or invent values for fields they didn't mention.
- intent: only set this if the utterance signals a genuine topic switch (e.g. they ask about the menu while \
ordering, or ask to order while browsing the menu) or this is the very first utterance. If they're just \
answering the current stage's question, leave intent null — don't re-state the current intent.
- Numbers may be spoken as words ("two", "five") — always convert them to actual integers for order_table \
and qty. Never leave a number as a word.
- modifications: ONLY extract a modification if the guest explicitly mentioned one ("spicy", "large", "no \
onions") — never invent or assume one just because a dish could have options. An item with no mentioned \
modification should have an empty modifications dict.
- If multiple distinct dishes are named in one utterance (e.g. "two pizzas and a coke"), return one entry \
per dish in item_changes — never merge them into a single entry, never drop any.
- Treat informal affirmatives ("yeah", "yep", "sure") as true and informal negatives ("nah", "nope", "not \
really") as false for wants_more/confirmed, without requiring the literal words.
- response_text is ONLY for genuine unrelated small talk (a brief, warm, natural reply) — leave it an empty \
string otherwise. Never wrap it in quotation marks.
- Return valid JSON only, no other text. Every string value must be double-quoted.
"""


def _coerce_intent(raw: Optional[str]) -> Optional[Intent]:
    if not raw:
        return None
    try:
        return Intent(raw)
    except ValueError:
        return None


def extract(
    llm: LLMClient,
    transcript: str,
    payload: Payload,
    expects: str,
) -> Dict[str, Any]:
    """Returns a raw dict: {intent, order_table, current_menu_category,
    wants_more, confirmed, item_changes, response_text}. Caller (main loop)
    is responsible for running item_changes through
    payload_manager.resolve_item_changes() before merge()."""
    system = build_system_prompt(payload, expects)
    history_tail = "\n".join(
        f"{turn['role']}: {turn['text']}" for turn in payload.conversation_history[-6:]
    )
    user = f"Recent conversation:\n{history_tail or '(nothing yet)'}\n\nLatest utterance: {transcript}"

    raw = llm.chat_json(system, user)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {"item_changes": []}

    result: Dict[str, Any] = {
        "intent": _coerce_intent(data.get("intent")),
        "order_table": _safe_int(data.get("order_table")),
        "current_menu_category": data.get("current_menu_category") or None,
        "wants_more": data.get("wants_more") if isinstance(data.get("wants_more"), bool) else None,
        "confirmed": data.get("confirmed") if isinstance(data.get("confirmed"), bool) else None,
        "menu_done": data.get("menu_done") if isinstance(data.get("menu_done"), bool) else None,
        "item_changes": _clean_item_changes(data.get("item_changes")),
        "response_text": (data.get("response_text") or "").strip(),
    }
    return result


def _safe_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _clean_item_changes(raw: Any) -> list:
    if not isinstance(raw, list):
        return []
    cleaned = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        qty = _safe_int(entry.get("qty"))
        mods = entry.get("modifications")
        mods = {str(k): str(v) for k, v in mods.items()} if isinstance(mods, dict) else {}
        if not name and qty is None and not mods:
            continue
        cleaned.append({"name": name, "qty": qty, "modifications": mods})
    return cleaned
