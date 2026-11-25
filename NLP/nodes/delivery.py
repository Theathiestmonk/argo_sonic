"""Delivery: at the table, announce the order, wait for "taken/done", then return to the home waypoint.

Entered (a) right after navigation with task=deliver ("go to table 4 and deliver the order"),
(b) from "deliver the order" when the robot already stands at a table, (c) as the reply to `deliver_done`.
"""
from __future__ import annotations

import logging

from ..state import State
from .ctx import Ctx
from .navigation import return_home

log = logging.getLogger("nlp.delivery")


def make_delivery(c: Ctx):
    async def finish(state: State):
        c.voice.say("Enjoy your meal!")
        home = await return_home(c, state.get("location"))
        out = {"awaiting": None, "task": None}
        if home:
            out["location"] = home
            out["table"] = None
        return out

    async def delivery(state: State):
        e = state.get("ent", {})
        loc, table = state.get("location"), state.get("table")
        if state.get("awaiting") == "deliver_done" and not e.get("arrived"):
            return await finish(state)
        if not e.get("arrived"):
            if not (loc and table is not None):             # "deliver the order" but we are not at a table
                c.voice.say("Which table should I deliver to?")
                return {"awaiting": "nav_location", "task": "deliver"}
        who = f"table {table}" if table is not None else (loc or "you")
        c.voice.say(f"Hello {who}, your order is here. Please take your items from the tray, and say done when you have them.")
        return {"awaiting": "deliver_done", "task": None}

    return delivery, finish
