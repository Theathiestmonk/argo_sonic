"""wake → greet → listen → understand (+ small utility nodes)."""
from __future__ import annotations

import logging
import re
import time

from .. import phrases as P
from ..fastpath import blank_ent, try_fast
from ..knowledge import norm, similarity
from ..state import AWAIT_OWNER, State
from .ctx import Ctx

log = logging.getLogger("nlp.core")


def _substantive(e: dict) -> bool:
    return bool(e.get("items") or e.get("location") or e.get("table_number") is not None or e.get("menu_category")
                or e.get("menu_item") or e.get("menu_query") or e.get("movement_direction") or e.get("wants_specials")
                or e.get("wants_famous") or e.get("general_topic") or e.get("remove_items") or e.get("cancel_order"))


def _infer_intent(e: dict, awaiting: str | None) -> str:
    if e.get("movement_direction"):
        return "movement"
    if e.get("items") or e.get("cancel_order") or e.get("remove_items"):
        return "ordering"
    if e.get("table_number") is not None and awaiting and AWAIT_OWNER.get(awaiting) == "ordering":
        return "ordering"
    if e.get("location"):
        return "navigation"
    if e.get("menu_category") or e.get("menu_item") or e.get("menu_query") or e.get("wants_specials") or e.get("wants_famous"):
        return "menu"
    if e.get("general_topic"):
        return "general"
    return "unclear"


_NUM = re.compile(r"^(?:table |number |no |for |i want |we want |make it |just |about |it is |its |we are at )*(\d{1,2})(?: please| only| each| of them| pieces| plates)?$")


def make_core(c: Ctx):
    def fast_slot(text: str, aw: str | None, state: State):
        """Answers to the robot's own slot questions, resolved locally (no LLM round-trip)."""
        t = norm(text)
        e = blank_ent()
        if aw in ("order_qty", "order_table"):
            m = _NUM.match(t)
            n = int(m.group(1)) if m else (1 if aw == "order_qty" and t in ("a", "an", "single", "just one") else None)
            if n is not None and n > 0:
                if aw == "order_qty":
                    e.update(intent="ordering", items=[{"name": None, "quantity": n, "variation": None}])
                else:
                    e.update(intent="ordering", table_number=n)
                return e
        elif aw == "order_variation":
            ln = next((p for p in state.get("pending", []) if p.get("var") and not p.get("variation")), None)
            if ln:
                for o in ln["var"]["options"]:
                    if similarity(t, o) >= 0.8 or o.lower() in t.split():
                        e.update(intent="ordering", items=[{"name": None, "quantity": None, "variation": o}])
                        return e
        elif aw in ("nav_location", "nav_confirm"):
            name, _ = c.wps.resolve(text)
            if name:
                e.update(intent="navigation", location=text)
                return e
        elif aw == "menu_category":
            if c.menu.resolve_categories(text):
                e.update(intent="menu", menu_category=text)
                return e
        elif aw == "menu_item":
            it, _ = c.menu.find_item(text, threshold=0.85)
            if it:
                e.update(intent="menu", menu_item=text)
                return e
        return None

    async def wake(state: State):
        await c.voice.wait_wake()
        if state.get("resumed"):
            # Barge-in mid-session: same customer, so the cart (order/pending/table/location) survives.
            return {"resumed": False, "awaiting": None, "silent_turns": 0, "suggest_line": None,
                    "menu_focus": None, "menu_category": None, "intent": "", "utterance": ""}
        # New customer interaction: forget the old order/dialogue, but keep where the robot physically is.
        return {"awaiting": None, "silent_turns": 0, "order": [], "pending": [], "suggest_line": None,
                "menu_focus": None, "menu_category": None, "intent": "", "utterance": ""}

    async def greet(state: State):
        c.voice.say(P.pick(P.GREETINGS))
        return {}

    async def listen(state: State):
        text = await c.voice.listen(c.s.listen_timeout_s)
        if not text:
            return {"utterance": "", "silent_turns": state.get("silent_turns", 0) + 1}
        return {"utterance": text, "silent_turns": 0}

    def after_listen(state: State) -> str:
        if state.get("utterance"):
            return "understand"
        return "silence_prompt" if state.get("silent_turns", 0) < 2 else "sleep"

    async def silence_prompt(state: State):
        # first silence → nudge (cached audio) and listen again
        if state.get("silent_turns") == 1 and not state.get("utterance"):
            c.voice.chirp()                                  # the shield's alert beep instead of words
        return {}

    async def understand(state: State):
        text = state["utterance"]
        aw = state.get("awaiting")
        t0 = time.perf_counter()
        ent = try_fast(text, aw) or (fast_slot(text, aw, state) if aw else None)
        src = "fast"
        if ent is None:
            src = "llm"
            ctx = {"awaiting": aw, "robot_at": state.get("location"),
                   "order": [l["name"] for l in state.get("order", [])] or None,
                   "pending": [p["name"] for p in state.get("pending", [])] or None}
            try:
                ent = await c.llm.classify(text, {k: v for k, v in ctx.items() if v is not None})
            except Exception as e:
                log.warning("classify failed: %s", e)
                c.voice.say(P.pick(P.ERROR))
                return {"intent": "unclear", "ent": {}}
        intent = ent.get("intent", "unclear")
        reply = ent.get("reply", "none")
        if aw and intent == "end" and not re.search(r"\b(bye|goodbye|good bye|see you|sleep)\b", text.lower()):
            intent, reply = AWAIT_OWNER.get(aw, "unclear"), "no"        # "that's all" mid-flow answers the question, it doesn't end the session
            ent = {**ent, "reply": "no"}
        if aw and reply in ("yes", "no") and not _substantive(ent):
            intent = AWAIT_OWNER.get(aw, intent)
        elif intent == "unclear":
            intent = _infer_intent(ent, aw)
        log.info("understood [%s %.0f ms] %r → %s %s", src, (time.perf_counter() - t0) * 1000, text, intent,
                 {k: v for k, v in ent.items() if v not in (None, [], False, "none", "")})
        c.menu.maybe_refresh()
        return {"intent": intent, "ent": ent}

    def route(state: State) -> str:
        return {"navigation": "navigation", "ordering": "ordering", "movement": "movement", "menu": "menu",
                "general": "general", "end": "farewell"}.get(state.get("intent", ""), "unclear")

    async def unclear(state: State):
        c.voice.say(P.pick(P.NOT_HEARD))
        return {}

    async def farewell(state: State):
        c.voice.say("Goodbye! Call me anytime.")
        return {"awaiting": None}

    async def sleep(state: State):
        c.voice.say(P.pick(P.SLEEP))
        return {"awaiting": None}

    return dict(wake=wake, greet=greet, listen=listen, after_listen=after_listen, silence_prompt=silence_prompt,
                understand=understand, route=route, unclear=unclear, farewell=farewell, sleep=sleep)
