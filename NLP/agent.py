"""Wires everything together and runs the graph."""
from __future__ import annotations

import asyncio
import logging

from . import phrases as P
from .config import Settings
from .graph import build_graph
from .knowledge import Menu, Restaurant, Waypoints
from .llm import RULES, LLM
from .nodes.ctx import Ctx
from .robot.orders import make_sink

log = logging.getLogger("nlp.agent")


async def build_ctx(s: Settings, voice, robot, sink=None) -> Ctx:
    rest = Restaurant(s)
    menu = Menu(s, rest)
    wps = Waypoints(s, rest.data.get("location_aliases"))
    rules = RULES.format(
        bot=rest.bot_name, rest=rest.name, waypoints=", ".join(wps.names),
        categories=", ".join(menu.categories), items="; ".join(i.name for i in menu.items),
        service=", ".join(x["name"] for x in rest.data.get("service_items", [])))
    llm = LLM(s, rules)
    return Ctx(s=s, voice=voice, llm=llm, menu=menu, rest=rest, wps=wps, robot=robot, sink=sink or make_sink(s, lambda: wps.map_name))


async def run(s: Settings, voice, robot, sink=None) -> None:
    c = await build_ctx(s, voice, robot, sink)
    fixed = P.ALL_FIXED + c.rest.data.get("upsell_lines", [])
    # bring up mic/speaker/ROS and warm the LLM connection concurrently – boot ≈ slowest of the three
    await asyncio.gather(voice.start(fixed), robot.start(), c.llm.warmup())
    graph = build_graph(c)
    log.info("ready – say 'Hi Sonic'")
    try:
        state = dict(_FRESH)
        while state is not None:
            state = await _run_once(graph, voice, state)
    finally:
        await robot.close()
        await voice.close()


_FRESH = {"order": [], "pending": [], "awaiting": None, "table": None, "location": None}


async def _run_once(graph, voice, state: dict) -> dict | None:
    """One graph run. If the wake word is heard while it is busy (talking, driving, listening …) the run is
    cancelled – which stops speech, cancels the Nav2 goal / halts a move, aborts STT and the LLM – and the last
    committed state is returned (cart, table, location kept) so the caller restarts the graph with it;
    voice.wait_wake() then returns immediately. None = graph ended."""
    latest = dict(state)

    async def drive():
        async for latest_vals in graph.astream(state, config={"recursion_limit": 10**9}, stream_mode="values"):
            latest.clear()
            latest.update(latest_vals)

    run = asyncio.create_task(drive())
    barge = asyncio.create_task(voice.barge_in())
    try:
        await asyncio.wait({run, barge}, return_when=asyncio.FIRST_COMPLETED)
        if run.done():
            run.result()                                 # propagate EOFError / real failures
            return None
        log.info("wake word during a session – stopping current action and restarting (cart kept)")
        run.cancel()
        await asyncio.gather(run, return_exceptions=True)
        voice.interrupt()
        return {**latest, "resumed": True}
    finally:
        for t in (run, barge):
            t.cancel()
