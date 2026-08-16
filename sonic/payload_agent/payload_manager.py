"""Mutating orchestration around Payload — merging extraction output in,
deciding when conversation history resets, and advancing turn-local state
(like opening a fresh order-item slot after "anything else?" -> yes).

Payload itself (models.py) stays a plain data container with read-only
queries; everything that changes a Payload across a turn lives here.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from models import Intent, OrderItem, Payload


def should_clear_history(old: Payload, extracted: Dict[str, Any]) -> bool:
    """True if this turn's extraction represents a genuine intent switch or
    a correction to a different table — either one means the old
    conversation context no longer applies."""
    new_intent = extracted.get("intent")
    if (
        new_intent
        and new_intent != Intent.NONE
        and old.intent != Intent.NONE
        and new_intent != old.intent
    ):
        return True

    new_table = extracted.get("order_table")
    if new_table is not None and old.order_table is not None and new_table != old.order_table:
        return True

    return False


def resolve_item_changes(
    payload: Payload,
    item_changes: List[Dict[str, Any]],
    canonicalize: Optional[Callable[[str], Optional[str]]] = None,
) -> Dict[int, Dict[str, Any]]:
    """Turn extraction's name-addressed item_changes (what the guest said,
    e.g. [{"name": "Coffee", "qty": 1}]) into the serial_no-keyed dict
    merge() expects — resolving each change against EXISTING order_items by
    name first, so "actually just one coffee" updates the coffee item even
    when a cookie was added more recently (not just "the last item").

    canonicalize(name) -> menu's own spelling, or None if no menu match;
    pass menu_loader.canonical_name in production. Falls back to the raw
    name when omitted (used by tests / non-menu intents).

    Match order:
      1. named item matches an existing item by (canonical) name -> update it
      2. no name given (e.g. a bare "three" answering "how many?") -> the
         most recently added INCOMPLETE item (the one get_next_question()
         is currently asking about), not just a nameless slot — an item can
         already have a name and still be incomplete (qty, or an unmatched
         name still awaiting clarification)
      3. named item with no exact match -> an existing CORRECTABLE slot
         (nameless, or named but not menu_matched — e.g. answering the
         "we don't have plain coffee, did you mean...?" clarification
         corrects the same slot rather than adding a new one), else new slot

    Each resolved entry also carries "menu_matched": True/False whenever a
    name is set, from whether canonicalize() found a real menu item —
    merge() propagates this onto OrderItem.menu_matched, which
    is_complete() requires. Without this, an item like "coffee" that
    doesn't match any specific menu SKU would silently reach confirmation
    and get dropped from the order at execute time instead of being
    caught and clarified.
    """
    canon = canonicalize or (lambda n: n)
    resolved: Dict[int, Dict[str, Any]] = {}
    next_sn = max(payload.order_items, default=0) + 1

    for change in item_changes:
        name = change.get("name")
        matched_name = canon(name) if name else None
        target_sn = None

        if name:
            for sn, item in payload.order_items.items():
                if sn in resolved:
                    continue  # already claimed by an earlier change this turn
                if item.name and item.name.lower() == (matched_name or name).lower():
                    target_sn = sn
                    break
            if target_sn is None:
                correctable = sorted(
                    sn for sn, item in payload.order_items.items()
                    if (not item.name or not item.menu_matched) and sn not in resolved
                )
                if correctable:
                    target_sn = correctable[0]
        else:
            incomplete = sorted(
                sn for sn, item in payload.order_items.items()
                if not item.is_complete() and sn not in resolved
            )
            if incomplete:
                target_sn = incomplete[-1]

        if target_sn is None:
            target_sn = next_sn
            next_sn += 1

        entry = resolved.setdefault(target_sn, {})
        if name:
            entry["name"] = matched_name or name
            entry["menu_matched"] = matched_name is not None
        if change.get("qty") is not None:
            entry["qty"] = change["qty"]
        if change.get("modifications"):
            entry.setdefault("modifications", {}).update(change["modifications"])

    return resolved


def merge(old: Payload, extracted: Dict[str, Any]) -> Payload:
    """Merge one turn's extraction output into the running Payload.

    On an intent or table change, order_items/current_menu_category/
    wants_more/confirmed reset (they're specific to the old intent's
    in-progress work) but order_table/robot_location carry forward as
    physical facts about the session — UNLESS the table itself is what
    changed, in which case starting the order fresh for the new table
    is safer than silently reattributing old items to it.
    """
    clear = should_clear_history(old, extracted)
    table_changed = (
        extracted.get("order_table") is not None
        and old.order_table is not None
        and extracted.get("order_table") != old.order_table
    )

    if clear:
        base = Payload(
            order_table=old.order_table if not table_changed else None,
            robot_location=old.robot_location,
        )
    else:
        base = old.model_copy(deep=True)

    if extracted.get("intent") and extracted["intent"] != Intent.NONE:
        base.intent = Intent(extracted["intent"])
    if extracted.get("order_table") is not None:
        base.order_table = extracted["order_table"]
    if extracted.get("robot_location") is not None:
        base.robot_location = extracted["robot_location"]
    new_category = extracted.get("current_menu_category")
    if new_category and new_category != base.current_menu_category:
        # A genuinely different category — describe it fresh and re-open
        # the "another category, order, or done?" question for it.
        base.current_menu_category = new_category
        base.menu_category_described = False
        base.menu_done = None
    if extracted.get("wants_more") is not None:
        base.wants_more = extracted["wants_more"]
    if extracted.get("menu_done") is not None:
        base.menu_done = extracted["menu_done"]
    if extracted.get("confirmed") is not None:
        base.confirmed = extracted["confirmed"]

    for sn_raw, item_data in extracted.get("order_items", {}).items():
        sn = int(sn_raw)
        if sn in base.order_items:
            existing = base.order_items[sn]
            if item_data.get("name"):
                existing.name = item_data["name"]
                existing.menu_matched = item_data.get("menu_matched", False)
            if item_data.get("qty") is not None:
                existing.qty = item_data["qty"]
            if item_data.get("modifications"):
                existing.modifications.update(item_data["modifications"])
        else:
            base.order_items[sn] = OrderItem(
                serial_no=sn,
                name=item_data.get("name"),
                qty=item_data.get("qty"),
                modifications=dict(item_data.get("modifications") or {}),
                menu_matched=item_data.get("menu_matched", False),
            )

    if base.intent == Intent.NONE and any(item.name for item in base.order_items.values()):
        # Naming a dish IS a take_order request even if the model failed to
        # set intent explicitly (live-observed with gpt-4o-mini: "can I have
        # two coffees?" extracted item_changes but left intent null despite
        # the prompt saying to set it) — without this, the guest's order
        # gets silently captured into a payload that never progresses past
        # intent==NONE, and get_next_question() just re-asks the wake-ack
        # forever. Naming a real dish is unambiguous enough to infer from
        # structurally rather than depend entirely on prompt compliance.
        base.intent = Intent.TAKE_ORDER

    if clear:
        base.conversation_history = []

    return base


def mark_category_described(payload: Payload) -> None:
    """Call after speaking a category's description (get_next_question()
    returned "describe_category") — flips the flag so the next
    get_next_question() call moves on to "menu_next_step" instead of
    re-describing the same category forever."""
    payload.menu_category_described = True


def advance(payload: Payload, item_named_this_turn: bool = False) -> Payload:
    """Apply state transitions that don't need new guest input — currently
    just: after "anything else?" -> yes, open a fresh item slot so
    get_next_question() asks for the new item's name next.

    item_named_this_turn must be True when this same turn's item_changes
    already named a dish (e.g. "Yes, one cookie" answers wants_more AND
    supplies the item in one breath — resolve_item_changes()/merge() will
    already have added/completed it). Skipping the new slot in that case
    matters: opening one anyway would ask "what dish?" a second, redundant
    time for an item the guest already gave. wants_more is reset to None
    either way once handled, so get_next_question() re-evaluates fresh —
    landing back on "anything_else" if the item they just named is already
    complete, or "item_qty"/etc. if it still needs more.

    Call this right after merge(), before get_next_question()."""
    if payload.intent == Intent.TAKE_ORDER and payload.wants_more is True:
        if not item_named_this_turn:
            next_sn = max(payload.order_items, default=0) + 1
            payload.order_items[next_sn] = OrderItem(serial_no=next_sn)
        payload.wants_more = None
    return payload


def append_history(payload: Payload, role: str, text: str) -> None:
    payload.conversation_history.append({"role": role, "text": text})


def calculate_total(payload: Payload, price_lookup) -> float:
    """price_lookup(item_name: str) -> float, e.g. menu_loader.get_price."""
    total = 0.0
    for item in payload.order_items.values():
        if not item.name or not item.qty:
            continue
        price = price_lookup(item.name)
        if price is not None:
            total += price * item.qty
    return total
