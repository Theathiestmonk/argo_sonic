"""Offline harness: scripted voice + scripted NLU, real graph / knowledge / state machines."""
from __future__ import annotations

import asyncio
from typing import AsyncIterator, Optional

from ..agent import build_ctx
from ..config import Settings
from ..fastpath import blank_ent
from ..graph import build_graph
from ..robot.base import MoveResult
from ..robot.mock import MockRobot


class Done(Exception):
    pass


class ScriptedVoice:
    def __init__(self, utterances: list[str]):
        self.script = list(utterances)
        self.said: list[str] = []
        self.wakes = 0
        self._q: list = []

    async def start(self, fixed): ...
    async def close(self): ...

    async def wait_wake(self):
        await self.drain()
        self.wakes += 1
        if self.wakes > 1:
            raise Done

    def say(self, t): self._q.append(t)
    def say_stream(self, it): self._q.append(it)
    def chirp(self): self._q.append("<chirp>")
    def interrupt(self): self._q.clear()
    async def barge_in(self): await asyncio.Event().wait()

    async def drain(self):
        q, self._q = self._q, []
        for it in q:
            if isinstance(it, str):
                self.said.append(it)
            else:
                self.said.append("".join([t async for t in it]))

    async def listen(self, timeout) -> Optional[str]:
        await self.drain()
        if not self.script:
            raise Done
        return self.script.pop(0) or None


class FakeSink:
    def __init__(self): self.orders = []
    async def submit(self, table, lines):
        self.orders.append((table, lines)); return "ref"


def ent(**kw) -> dict:
    return {**blank_ent(), **kw}


async def run_script(script: list[tuple[str, dict]], robot=None, s: Optional[Settings] = None):
    """script = [(utterance, entities-the-LLM-would-return)]; fast paths still run first, like production."""
    from ..config import load_settings
    s = s or load_settings()
    voice = ScriptedVoice([u for u, _ in script])
    robot = robot or MockRobot(nav_seconds=0)
    sink = FakeSink()
    c = await build_ctx(s, voice, robot, sink)
    table = {u: e for u, e in script}

    async def classify(text, ctx):
        if text not in table:
            raise AssertionError(f"unexpected LLM call for {text!r} ctx={ctx}")
        return table[text]

    async def stream(system, user, **kw) -> AsyncIterator[str]:
        for w in f"[LLM:{user}] ok.".split(" "):
            yield w + " "
    c.llm.classify, c.llm.stream = classify, stream
    graph = build_graph(c)
    try:
        await graph.ainvoke({"order": [], "pending": [], "awaiting": None, "table": None, "location": None},
                            config={"recursion_limit": 10**6})
    except Done:
        pass
    return voice, robot, sink
