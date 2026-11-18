"""Ordering – a deterministic slot-filling state machine (no LLM call on this node = lowest latency).

table → item → quantity → variation → "anything else?" → read-back → send to the table's UI.
Slots the user already gave (even all in one sentence) are never asked again.
"""
from __future__ import annotations

import logging
from typing import Optional

from .. import phrases as P
from ..knowledge import similarity
from ..state import State
from .ctx import Ctx

log = logging.getLogger("nlp.order")


def _fmt(l: dict) -> str:
    v = f" ({l['variation']})" if l.get("variation") else ""
    return f"{l['qty']} {l['name']}{v}"


def _join(parts: list[str]) -> str:
    return parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]


def make_ordering(c: Ctx):
    say = c.voice.say

    # ---------------------------------------------------------------- item resolution
    def resolve_item(raw: str) -> tuple[str, Optional[dict]]:
        """-> (status, line|suggestion_line). status: ok | unavailable | suggest | unknown"""
        svc = c.rest.service_item(raw)
        if not svc:
            amb = c.menu.ambiguous(raw)
            if amb:
                return "ambiguous", {"options": [i.name for i in amb]}
        if svc:
            return "ok", {"name": svc, "kind": "service", "qty": None, "variation": None, "price": 0, "menu_id": None, "var": None}
        it, sug = c.menu.find_item(raw)
        if it:
            if not it.available:
                return "unavailable", {"name": it.name}
            return "ok", {"name": it.name, "kind": "menu", "qty": None, "variation": None, "price": it.offer_price(),
                          "menu_id": it.id, "var": it.variation}
        if sug and sug.available:
            return "suggest", {"name": sug.name, "kind": "menu", "qty": None, "variation": None, "price": sug.offer_price(),
                               "menu_id": sug.id, "var": sug.variation}
        return "unknown", None

    def canon_variation(line: dict, text: Optional[str]) -> Optional[str]:
        spec = line.get("var")
        if not text or not spec:
            return None
        best = max(spec["options"], key=lambda o: similarity(text, o))
        return best if similarity(text, best) >= 0.7 else None

    def apply(line: dict, qty, variation) -> None:
        if qty and line.get("qty") is None:
            line["qty"] = max(1, int(qty))
        if variation and line.get("variation") is None:
            line["variation"] = canon_variation(line, variation)

    def incomplete(line: dict) -> bool:
        if line["kind"] == "service":
            line["qty"] = line["qty"] or 1
            return False
        return line["qty"] is None or (line.get("var") and not line.get("variation"))

    # ---------------------------------------------------------------- node
    async def ordering(state: State):
        e = state.get("ent", {})
        reply = e.get("reply", "none")
        aw = state.get("awaiting")
        table = state.get("table")
        order = [dict(l) for l in state.get("order", [])]
        pending = [dict(l) for l in state.get("pending", [])]
        suggest = state.get("suggest_line")
        go_confirm = submit = False

        def out(awaiting, **kw):
            return {"awaiting": awaiting, "table": table, "order": order, "pending": pending,
                    "suggest_line": kw.pop("suggest_line", None), **kw}

        if e.get("cancel_order"):
            order, pending = [], []
            say("Okay, I've cancelled the order. Let me know if you want to order something else.")
            return out(None)

        # ---- table number (sticky once known)
        tn = e.get("table_number")
        if tn is not None:
            known = c.wps.table_numbers()
            if known and tn not in known:
                say(f"I don't see a table {tn}. Which table are you at?")
                return out("order_table")
            table = int(tn)

        # ---- answers to a pending yes/no question
        items = [i for i in (e.get("items") or [])]
        has_items = any(i.get("name") for i in items)
        if aw == "order_prompt":
            if reply == "no":
                say("No problem. Just call me when you're ready.")
                return out(None)
        elif aw == "order_suggest" and suggest:
            if reply == "yes":
                ln = dict(suggest)
                pending.append(ln)
                suggest = None
            elif reply == "no":
                say("Okay. Which dish would you like instead?")
                return out("order_item")
        elif aw == "order_more" and not has_items:
            if reply == "no":
                go_confirm = True
            elif reply == "yes":
                say(P.pick(P.ASK_ITEM))
                return out("order_item")
        elif aw == "order_confirm" and not has_items:
            if reply == "yes":
                submit = True
            elif reply == "no":
                say("Okay, what would you like to change? You can add or remove a dish, or say cancel.")
                return out("order_item")

        # ---- removals
        for r in e.get("remove_items") or []:
            hit = max(order, key=lambda l: similarity(r, l["name"]), default=None)
            if hit and similarity(r, hit["name"]) >= 0.7:
                order.remove(hit)
                say(f"Removed {hit['name']}.")
                go_confirm = go_confirm or not items

        # ---- merge newly heard items into pending lines
        new_suggest = None
        asked_which = False
        for it in items:
            name, qty, var = it.get("name"), it.get("quantity"), it.get("variation")
            if not name:
                tgt = next((p for p in pending if incomplete(p)), None)
                if tgt:
                    apply(tgt, qty, var)
                continue
            st, ln = resolve_item(name)
            if st == "ambiguous":
                if not asked_which:
                    say(f"We have {_join(ln['options'])}. Which one would you like?")
                    asked_which = True
            elif st == "unavailable":
                say(f"Sorry, {ln['name']} is not available right now.")
            elif st == "unknown":
                say(f"Sorry, I couldn't find {name} on our menu.")
            elif st == "suggest":
                ln["qty"] = qty
                new_suggest = ln
            else:
                dup = next((p for p in pending if p["name"] == ln["name"] and incomplete(p)), None)
                if dup:
                    apply(dup, qty, var)
                else:
                    apply(ln, qty, var)
                    pending.append(ln)
        if new_suggest:
            suggest = new_suggest
        if asked_which:                      # one question at a time – answer it before anything else
            return out("order_item", suggest_line=None)

        # ---- 1) table first
        if table is None:
            if aw != "order_table" or tn is None:
                say(P.pick(P.ASK_TABLE))
            return out("order_table", suggest_line=suggest)

        # ---- 2) clarify a suggested dish
        if suggest and not pending_blocking(pending, incomplete):
            say(f"Did you mean {suggest['name']}?")
            return out("order_suggest", suggest_line=suggest)

        # ---- 3) fill missing slots, one question at a time, qty → variation
        for ln in pending:
            if ln["kind"] == "menu" and ln["qty"] is None:
                say(f"How many {ln['name']} would you like?")
                return out("order_qty", suggest_line=suggest)
            if incomplete(ln):
                spec = ln["var"]
                opts = " or ".join(spec["options"])
                say(f"Which {spec['label']} would you like for the {ln['name']}? {opts}?")
                return out("order_variation", suggest_line=suggest)

        # ---- 4) commit completed lines
        added = []
        for ln in pending:
            ln = {k: v for k, v in ln.items() if k != "var"}
            same = next((o for o in order if o["name"] == ln["name"] and o.get("variation") == ln.get("variation")), None)
            if same:
                same["qty"] += ln["qty"]
            else:
                order.append(ln)
            added.append(ln)
        pending = []

        if submit:
            return await send(order, table, state)

        if added and not go_confirm:
            say(f"Added {_join([_fmt(l) for l in added])}. Anything else?")
            return out("order_more", suggest_line=suggest)

        if go_confirm:
            if not order:
                say("Your order is empty. What would you like to order?")
                return out("order_item")
            say(f"So that's {_join([_fmt(l) for l in order])} for table {table}. Shall I place the order?")
            return out("order_confirm")

        # nothing specific heard ("I want to order") → ask for the dish
        if not order:
            say(P.pick(P.ASK_ITEM))
            return out("order_item", suggest_line=suggest)
        say(P.pick(P.ASK_MORE))
        return out("order_more", suggest_line=suggest)

    def pending_blocking(pending, incomplete_fn) -> bool:
        return any(incomplete_fn(p) for p in pending)

    async def send(order: list[dict], table: int, state: State):
        try:
            ref = await c.sink.submit(table, order)
            log.info("order sent: table %s ref %s %s", table, ref, order)
        except Exception as ex:
            log.exception("order submit failed: %s", ex)
            say(P.pick(P.ORDER_SENT_FAIL))
            return {"awaiting": "order_confirm", "table": table, "order": order, "pending": [], "suggest_line": None}
        say(f"Done! Your order for table {table} has been sent to the kitchen. It will be with you shortly.")
        return {"awaiting": None, "table": table, "order": [], "pending": [], "suggest_line": None}

    return ordering
