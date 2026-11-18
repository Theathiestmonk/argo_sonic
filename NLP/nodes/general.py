"""General Q&A about the robot / restaurant / weather / jokes, then steer back to taking an order."""
from __future__ import annotations

import logging
import random

import httpx

from .. import phrases as P
from ..llm import PERSONA
from ..state import State
from .ctx import Ctx

log = logging.getLogger("nlp.general")

_WMO = {0: "clear", 1: "mostly clear", 2: "partly cloudy", 3: "cloudy", 45: "foggy", 48: "foggy", 51: "drizzling",
        53: "drizzling", 55: "drizzling", 61: "rainy", 63: "rainy", 65: "heavily raining", 80: "showery", 81: "showery",
        82: "heavily showery", 95: "stormy", 96: "stormy", 99: "stormy"}


async def _weather(loc: dict) -> str | None:
    try:
        async with httpx.AsyncClient(timeout=1.8) as h:
            r = await h.get("https://api.open-meteo.com/v1/forecast", params={
                "latitude": loc["lat"], "longitude": loc["lon"], "current": "temperature_2m,weather_code"})
            cur = r.json()["current"]
        return f"It's {round(cur['temperature_2m'])} degrees and {_WMO.get(cur['weather_code'], 'pleasant')} in {loc.get('city', 'town')} right now."
    except Exception as e:
        log.warning("weather failed: %s", e)
        return None


def make_general(c: Ctx):
    async def general(state: State):
        e, text = state.get("ent", {}), state.get("utterance", "")
        topic = e.get("general_topic")
        bot, rest = c.rest.bot_name, c.rest.name
        if topic == "name":
            c.voice.say(f"I'm {bot}, the robot waiter at {rest}.")
        elif topic == "weather" and (w := await _weather(c.rest.data.get("location", {}))):
            c.voice.say(w)
        else:
            if topic == "joke":
                sys_p = f"You are {bot}, a cheerful waiter robot. Tell ONE short, clean, family-friendly joke (max 2 sentences), food or robot themed. No preface."
                gen = c.llm.stream(sys_p, text or "tell me a joke", max_tokens=70, temperature=0.9)
            else:
                gen = c.llm.stream(PERSONA.format(bot=bot, rest=rest, facts=c.rest.facts()), text, max_tokens=110)
            c.voice.say_stream(gen)
        lines = c.rest.data.get("upsell_lines") or ["Now, are you ready to order?"]
        c.voice.say(random.choice(lines))          # appended behind the streamed answer
        return {"awaiting": "order_prompt"}

    return general
