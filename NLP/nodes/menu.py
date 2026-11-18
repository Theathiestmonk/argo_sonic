"""Menu: category → item → description → "want to order it?" – like a waiter. Specials/famous dishes come
from the menu data; open-ended questions use a small lexical RAG over the menu + one streamed LLM answer."""
from __future__ import annotations

import logging
from typing import Optional

from .. import phrases as P
from ..fastpath import _blank
from ..knowledge import MenuItem
from ..llm import PERSONA
from ..state import State
from .ctx import Ctx

log = logging.getLogger("nlp.menu")


def _names(items: list[MenuItem], limit: int = 8) -> str:
    n = [i.name for i in items[:limit]]
    return n[0] if len(n) == 1 else ", ".join(n[:-1]) + " and " + n[-1]


def make_menu(c: Ctx):
    say = c.voice.say

    def describe(it: MenuItem) -> str:
        diet = "vegetarian" if it.diet == "veg" else "non-vegetarian"
        deal = f" It's on offer right now: {it.discount} percent off." if it.discount else ""
        price = c.menu.money(it.offer_price())
        return f"{it.name}: {it.desc.rstrip('.')}. It's {diet} and costs {price}.{deal}"

    def handoff_to_order(item_name: str):
        """'yes, order it' → the ordering node takes over with that dish already filled in (graph edge checks intent)."""
        ent = {**_blank(), "intent": "ordering", "items": [{"name": item_name, "quantity": None, "variation": None}]}
        return {"ent": ent, "intent": "ordering", "awaiting": None, "menu_focus": None, "menu_category": None}

    def ask_category():
        say(f"We have {', '.join(c.menu.categories)}. Which category would you like to hear about?")
        return {"awaiting": "menu_category", "menu_focus": None}

    def list_category(cats: list[str], diet: Optional[str]):
        items = [i for cat in cats for i in c.menu.in_category(cat, diet)]
        label = " and ".join(cats)
        if not items:
            say(f"Sorry, nothing available in {label} right now. {'We have ' + ', '.join(c.menu.categories) + '. ' if c.menu.categories else ''}Which category would you like?")
            return {"awaiting": "menu_category"}
        say(f"Under {label} we have {_names(items)}. Which one would you like to know about?")
        return {"awaiting": "menu_item", "menu_category": label, "menu_focus": None}

    def one_item(it: MenuItem):
        if not it.available:
            alt = [i for i in c.menu.in_category(it.category) if i.id != it.id]
            extra = f" You could try {_names(alt, 3)}." if alt else ""
            say(f"Sorry, {it.name} is not available right now.{extra}")
            return {"awaiting": "menu_item" if alt else None}
        say(describe(it) + " Would you like to order it?")
        return {"awaiting": "menu_order", "menu_focus": it.name}

    async def menu(state: State):
        e, aw = state.get("ent", {}), state.get("awaiting")
        reply = e.get("reply", "none")
        focus = state.get("menu_focus")
        diet = e.get("diet")

        # ---- answers to the waiter's follow-up questions
        if aw == "menu_order" and focus:
            if reply == "yes":
                return handoff_to_order(focus)
            if reply == "no":
                say("No problem. Would you like to know about another item?")
                return {"awaiting": "menu_more", "menu_focus": None}
        if aw == "menu_more":
            if reply == "no":
                say("Alright! Let me know whenever you'd like to order.")
                return {"awaiting": None}
            if reply == "yes" and not (e.get("menu_item") or e.get("menu_category")):
                return ask_category()

        # ---- what exactly is asked
        if e.get("wants_specials"):
            sp = c.menu.specials()
            if sp:
                say(f"Today's specials are {_names(sp)}. Which one would you like to know about?")
                return {"awaiting": "menu_item", "menu_focus": None}
        if e.get("wants_famous"):
            fam = c.menu.famous() or c.menu.specials()
            if fam:
                say(f"Our most loved dishes are {_names(fam)}. {fam[0].name} is {fam[0].desc.lower().rstrip('.')}. Which one would you like to know about?")
                return {"awaiting": "menu_item", "menu_focus": None}

        item_q = e.get("menu_item")
        if item_q:
            amb = c.menu.ambiguous(item_q)
            if amb:
                say(f"We have {_names(amb)}. Which one would you like to know about?")
                return {"awaiting": "menu_item", "menu_focus": None}
            it, sug = c.menu.find_item(item_q)
            if it:
                return one_item(it)
            if sug:
                say(f"Did you mean {sug.name}?")
                return {"awaiting": "menu_item", "menu_focus": None}
            cats = c.menu.resolve_categories(item_q)       # "do you have burgers" lands here
            if cats:
                return list_category(cats, diet)

        cat_q = e.get("menu_category")
        if cat_q:
            cats = c.menu.resolve_categories(cat_q)
            if cats:
                return list_category(cats, diet)
            hit = c.menu.search(cat_q, k=5, diet=diet)
            if hit:
                say(f"I don't have a category called {cat_q}, but you may like {_names(hit)}. Which one would you like to know about?")
                return {"awaiting": "menu_item"}
            say(f"Sorry, we don't have {cat_q}.")
            return ask_category()

        q = e.get("menu_query") or (state.get("utterance") if diet else None)
        if q:
            hits = c.menu.search(q, k=6, diet=diet)
            if hits:
                sys_p = PERSONA.format(bot=c.rest.bot_name, rest=c.rest.name,
                                       facts="Relevant menu items:\n" + c.menu.rag_context(hits)) + \
                    "\nSuggest at most 2 matching dishes by name and mention what makes them fit. Do not invent dishes."
                c.voice.say_stream(c.llm.stream(sys_p, q, max_tokens=90))
                say("Would you like to order any of these?")
                return {"awaiting": "menu_item", "menu_focus": None}
            say("I couldn't find anything like that on our menu.")
            return ask_category()

        if diet:
            hits = [i for i in c.menu.items if i.available and i.diet == diet]
            say(f"We have lots of {'vegetarian' if diet == 'veg' else 'non-vegetarian'} dishes, like {_names(hits, 5)}. Which category would you like?")
            return {"awaiting": "menu_category"}
        return ask_category()

    return menu
