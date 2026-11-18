"""Movement: 1 m in the asked direction, or an in-place turn/spin. The safety shield stays in the loop
(commands go through it); we additionally pre-check clearance and report blocked paths in words."""
from __future__ import annotations

import asyncio
import logging

from .. import phrases as P
from ..state import State
from .ctx import Ctx

log = logging.getLogger("nlp.move")


def make_movement(c: Ctx):
    async def movement(state: State):
        d = state.get("ent", {}).get("movement_direction")
        if not d:
            c.voice.say("Which way should I move?")
            return {"awaiting": None}
        task = asyncio.create_task(c.robot.move(d))
        # A blocked path is reported by move() within a few ms – only announce "moving" if we really are.
        done, _ = await asyncio.wait({task}, timeout=0.15)
        if not done:
            c.voice.say(P.MOVING[d])
        try:
            res = await task
        except Exception as e:
            log.exception("move failed: %s", e)
            c.voice.say(P.pick(P.ERROR))
            return {"awaiting": None}
        if res.status == "blocked":
            log.info("blocked %s: %s", d, res.detail)
            c.voice.say(P.pick(P.BLOCKED))
        elif res.status == "no_sensors":
            c.voice.say(P.pick(P.NO_SENSORS))
        elif res.status != "ok":
            c.voice.say(P.pick(P.ERROR))
        # Moving off the waypoint invalidates "I'm at <place>", but the table stays sticky for ordering.
        return {"awaiting": None, "location": None if res.ok and d in ("forward", "backward", "left", "right") else state.get("location")}

    return movement
