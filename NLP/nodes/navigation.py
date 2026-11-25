"""Navigation: destination from the saved waypoints only; suggest the closest if unknown.

A command can carry a job for arrival ("go to table 1 and take order" / "... and deliver the order"):
the job lives in state["task"] (it survives clarification questions) and, once the robot has arrived,
navigation hands over to the `ordering` or `delivery` node by overriding state["intent"].
"""
from __future__ import annotations

import logging
import re

from .. import phrases as P
from ..state import State
from .ctx import Ctx

log = logging.getLogger("nlp.nav")

TASK_INTENT = {"take_order": "ordering", "deliver": "delivery"}


async def return_home(c: Ctx, at: str | None = None) -> str | None:
    """After a delivery: drive back to the configured home waypoint (if any). Returns its name when we got there."""
    name, _ = c.wps.resolve(c.s.home_waypoint) if c.s.home_waypoint else (None, None)
    if not name or name == at:
        return None
    c.voice.say(f"Heading back to {name}.")
    try:
        ok = await c.robot.navigate(c.wps.get(name))
    except Exception as e:
        log.exception("return home failed: %s", e)
        ok = False
    return name if ok else None


def make_navigation(c: Ctx):
    async def go(name: str, task: str | None = None):
        wp = c.wps.get(name)
        c.voice.say(f"Going to {name}.")                    # speech plays while we already drive
        try:
            ok = await c.robot.navigate(wp)
        except Exception as e:
            log.exception("navigate failed: %s", e)
            ok = False
        if not ok:
            c.voice.say(P.pick(P.NOT_AVAILABLE_NAV))
            return {"awaiting": None, "task": None}
        m = re.fullmatch(r"table\s*(\d+)", name, re.I)
        table = int(m.group(1)) if m else None
        out = {"location": name, "awaiting": None, "nav_suggestion": None, "task": None}
        if table is not None:
            out["table"] = table
        if task in TASK_INTENT:                             # arrived with a job → hand over to that node
            out["intent"] = TASK_INTENT[task]
            out["ent"] = {"task": task, "arrived": True}
            if task == "take_order":
                c.voice.say(f"I'm at {name}.")
        else:
            c.voice.say(f"I'm at {name}.")
        return out

    async def navigation(state: State):
        e, aw, reply = state.get("ent", {}), state.get("awaiting"), state.get("ent", {}).get("reply", "none")
        task = e.get("task") or state.get("task")
        q = e.get("location")
        if aw == "nav_confirm" and not q:
            if reply == "yes" and state.get("nav_suggestion"):
                return await go(state["nav_suggestion"], task)
            c.voice.say("Okay. Where would you like me to go?")
            return {"awaiting": "nav_location", "nav_suggestion": None, "task": task}
        if not q:
            c.voice.say(P.pick(P.ASK_LOCATION))
            return {"awaiting": "nav_location", "task": task}
        name, sugg = c.wps.resolve(q)
        if name:
            return await go(name, task)
        if sugg:
            c.voice.say(f"I'm sorry, I don't know how to go to {q}. Do you want me to go to {sugg} instead?")
            return {"awaiting": "nav_confirm", "nav_suggestion": sugg, "task": task}
        known = ", ".join(c.wps.names[:6])
        c.voice.say(f"I'm sorry, I couldn't find {q} on my map. I can go to {known}. Where would you like to go?")
        return {"awaiting": "nav_location", "nav_suggestion": None, "task": task}

    return navigation
