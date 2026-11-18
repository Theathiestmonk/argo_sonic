"""Navigation: destination from the saved waypoints only; suggest the closest if unknown."""
from __future__ import annotations

import logging
import re

from .. import phrases as P
from ..state import State
from .ctx import Ctx

log = logging.getLogger("nlp.nav")


def make_navigation(c: Ctx):
    async def go(name: str):
        wp = c.wps.get(name)
        c.voice.say(f"Going to {name}.")                    # speech plays while we already drive
        try:
            ok = await c.robot.navigate(wp)
        except Exception as e:
            log.exception("navigate failed: %s", e)
            ok = False
        if not ok:
            c.voice.say(P.pick(P.NOT_AVAILABLE_NAV))
            return {"awaiting": None}
        m = re.fullmatch(r"table\s*(\d+)", name, re.I)
        c.voice.say(f"I'm at {name}.")
        return {"location": name, "table": int(m.group(1)) if m else None, "awaiting": None, "nav_suggestion": None}

    async def navigation(state: State):
        e, aw, reply = state.get("ent", {}), state.get("awaiting"), state.get("ent", {}).get("reply", "none")
        q = e.get("location")
        if aw == "nav_confirm" and not q:
            if reply == "yes" and state.get("nav_suggestion"):
                return await go(state["nav_suggestion"])
            c.voice.say("Okay. Where would you like me to go?")
            return {"awaiting": "nav_location", "nav_suggestion": None}
        if not q:
            c.voice.say(P.pick(P.ASK_LOCATION))
            return {"awaiting": "nav_location"}
        name, sugg = c.wps.resolve(q)
        if name:
            return await go(name)
        if sugg:
            c.voice.say(f"I'm sorry, I don't know how to go to {q}. Do you want me to go to {sugg} instead?")
            return {"awaiting": "nav_confirm", "nav_suggestion": sugg}
        known = ", ".join(c.wps.names[:6])
        c.voice.say(f"I'm sorry, I couldn't find {q} on my map. I can go to {known}. Where would you like to go?")
        return {"awaiting": "nav_location", "nav_suggestion": None}

    return navigation
