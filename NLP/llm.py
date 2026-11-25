"""OpenAI wrapper: one strict-JSON call for intent+entities, plus token streaming for answers.

Latency notes
 - The system prompt is *static* (rules + waypoints + menu names) and the dynamic dialogue
   context is appended last, so OpenAI prompt-caching hits the whole prefix.
 - Output is a compact JSON (only the keys that apply, ~20 tokens) – output length is most of the latency.
 - Reasoning is switched off on gpt-5.x models.
"""
from __future__ import annotations

import json
import logging
import re
from typing import AsyncIterator, Optional

from openai import AsyncOpenAI, BadRequestError, NotFoundError, PermissionDeniedError

from .config import Settings
from .fastpath import blank_ent

log = logging.getLogger("nlp.llm")

RULES = """You are the language-understanding unit of {bot}, a waiter robot at {rest}. Users speak English, Hindi or Hinglish; the text comes from speech recognition (may contain errors).
Return ONE JSON object with the intent and EVERY entity you can find. Include ONLY the keys that apply – omit nulls, empty lists and false values.
Keys: intent (navigation|ordering|delivery|movement|menu|general|end|unclear), reply (yes|no), location, table_number (int), items [{{name, quantity(int), variation}}], movement_direction (forward|backward|left|right|spin|turn_left|turn_right), menu_category, menu_item, menu_query, diet (veg|nonveg), wants_specials (bool), wants_famous (bool), general_topic (name|weather|joke|restaurant|other), remove_items [str], cancel_order (bool), task (take_order|deliver).

INTENTS
- navigation: any "go to / take me to / come to <place>" command. Put the place in `location` exactly as the user said it (null if no place was said).
- navigation WITH a job: "go to table 1 and take order" -> intent navigation, location "table 1", task take_order. "go to table 4 and deliver the order / serve the food / bring the food" -> intent navigation, location "table 4", task deliver. Hindi/Hinglish too ("table 3 pe jao aur order lo"). Without a place ("take order", "deliver the order") leave location out and still set task.
- delivery: only for replies at the table after the robot delivered (awaiting deliver_done): "taken", "got it", "thank you".
- ordering: wants to order/buy/get food or an item: "I want to order", "take my order", "bring me water/towel/ketchup", "two butter chicken". Fill `items` (name, quantity, variation – null when not said; quantity as integer) and `table_number` if a table is mentioned.
- movement: move/turn/spin commands without a destination (move left/right/back/forward, spin, turn left/right). Set `movement_direction`.
- menu: asks about the menu, categories, specials, famous dishes, ingredients, price, veg/non-veg, or any particular dish ("what's on the menu", "what are the snack options", "tell me about the biryani", "what is special today"). Fill `menu_category` (the category word as said, e.g. burgers, snacks, drinks), `menu_item` (a specific dish asked about), `wants_specials` (today's special/offers/deals), `wants_famous` (famous/popular/best-selling/signature), `diet`, and `menu_query` (the free-text question when it is not a plain category/item ask, e.g. "something spicy with chicken").
- general: name, weather, jokes, the restaurant (timings, address, wifi, payment...), chit-chat. Set `general_topic`.
- end: the user is done / says bye.
- unclear: cannot tell.

CONTEXT RULES
- `awaiting` tells what the robot just asked. Interpret short replies in that light: a bare number when awaiting order_qty is a quantity (items:[{{name:null,quantity:N}}]); when awaiting order_table it is `table_number`; a bare size/spice word when awaiting order_variation is a variation (items:[{{name:null,quantity:null,variation:W}}]); a place name when awaiting nav_location is `location`; a category when awaiting menu_category is `menu_category`; a dish when awaiting menu_item is `menu_item`.
- Yes/no replies: set `reply` to yes/no and keep intent = the intent that owns `awaiting` (or "unclear" if nothing awaited). If the user says yes AND adds content ("yes and a coke") fill the entities too.
- If the user changes topic, follow the new topic and ignore `awaiting`.
- `remove_items` lists dishes the user wants removed from the current order; `cancel_order` is true for "cancel my order / start over".
- Never invent entities. Use null / [] / false when absent.

(Destination names are validated against the live navigation map elsewhere – just report the place as the user said it.)
MENU CATEGORIES: {categories}
MENU ITEMS: {items}
SERVICE ITEMS (free extras that can be ordered): {service}
"""


def _kw(model: str, max_tokens: int, temperature: float | None = None) -> dict:
    """Param names differ between model families (gpt-5.x/o-series reasoning models vs gpt-4o)."""
    if model.startswith(("gpt-5", "o1", "o3", "o4")):
        kw = {"max_completion_tokens": max_tokens}
        if model.startswith("gpt-5"):
            kw["reasoning_effort"] = "none" if re.match(r"gpt-5\.\d", model) else "minimal"   # no thinking: speed
        return kw           # (these models only accept the default temperature)
    kw = {"max_tokens": max_tokens}
    if temperature is not None:
        kw["temperature"] = temperature
    return kw


class LLM:
    def __init__(self, s: Settings, system_rules: str):
        self.s = s
        self.client = AsyncOpenAI(api_key=s.openai_api_key, timeout=s.llm_timeout_s, max_retries=1)
        self.system_rules = system_rules
        self.models = [s.llm_model, *[m for m in s.llm_fallbacks if m != s.llm_model]]
        self.model = self.models[0]

    async def _call(self, build):
        """Run `build(model)`; if the model is gone/unsupported, fall to the next one and remember it."""
        last = None
        for i, m in enumerate(self.models):
            if i < self.models.index(self.model):
                continue
            try:
                r = await build(m)
                self.model = m
                return r
            except (NotFoundError, BadRequestError, PermissionDeniedError) as e:
                log.warning("model %s unusable (%s) – trying next", m, str(e)[:120])
                last = e
        raise last

    async def warmup(self) -> None:
        """Opens the TLS connection + primes the prompt cache so the first real turn is fast."""
        try:
            await self.classify("hello", {})
        except Exception as e:
            log.warning("llm warmup failed: %s", e)

    async def classify(self, utterance: str, ctx: dict) -> dict:
        user = f"CONTEXT: {json.dumps(ctx, ensure_ascii=False)}\nUSER: {utterance}"

        async def build(m):
            return await self.client.chat.completions.create(
                model=m, messages=[{"role": "system", "content": self.system_rules}, {"role": "user", "content": user}],
                response_format={"type": "json_object"}, **_kw(m, 200, 0))
        r = await self._call(build)
        try:
            d = json.loads(r.choices[0].message.content)
        except Exception:
            d = {}
        out = {**blank_ent(), **{k: v for k, v in d.items() if v is not None}}
        out["items"] = [{"name": i.get("name"), "quantity": i.get("quantity"), "variation": i.get("variation")}
                        for i in (out.get("items") or []) if isinstance(i, dict)]
        return out

    async def stream(self, system: str, user: str, history: Optional[list[dict]] = None,
                     max_tokens: int = 160, temperature: float = 0.6) -> AsyncIterator[str]:
        msgs = [{"role": "system", "content": system}, *(history or []), {"role": "user", "content": user}]

        async def build(m):
            return await self.client.chat.completions.create(model=m, messages=msgs, stream=True, **_kw(m, max_tokens, temperature))
        stream = await self._call(build)
        async for chunk in stream:
            if chunk.choices:
                d = chunk.choices[0].delta.content
                if d:
                    yield d


PERSONA = """You are {bot}, a friendly, quick-witted waiter robot at {rest}. You speak aloud, so: at most 2 short sentences, plain words, no lists, no markdown, no emojis. Answer ONLY from the facts below when asked about the restaurant; if you don't know, say so briefly. Match the user's language (English/Hindi/Hinglish).

{facts}
"""
