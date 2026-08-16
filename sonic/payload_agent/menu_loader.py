"""Loads the live menu from Postgres once at startup and serves lookups —
price, fuzzy item-name matching, category matching, spoken descriptions.
Same schema/tables main_agent.py uses (menu_items/menu_categories/
menu_settings), reloaded independently here so payload_agent doesn't
import main_agent's module-level state.
"""

from __future__ import annotations

import difflib
import sys
from typing import Dict, List, Optional

import db as db_module
from db import db

MENU_ITEMS: List[dict] = []
MENU_LOOKUP: Dict[str, dict] = {}
CURRENCY_SYMBOL = "₹"  # this deployment is INR-only for now; menu_settings.currency_code overrides below

_CURRENCY_SYMBOLS = {"USD": "$", "INR": "₹", "EUR": "€", "GBP": "£"}


def load_menu() -> None:
    global MENU_ITEMS, MENU_LOOKUP, CURRENCY_SYMBOL
    conn = db()
    location_id = db_module.DB_LOCATION_ID
    if conn is None or location_id is None:
        print("No database/location available — menu can't be loaded. Set DATABASE_URL, "
              "then run run_migrations.py and seed_db.py.")
        sys.exit(1)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT mi.menu_item_id, mi.item_name, mc.name, mi.price, mi.is_available, mi.description
                   FROM menu_items mi
                   LEFT JOIN menu_categories mc ON mi.category_id = mc.category_id
                   WHERE mi.location_id = %s""",
                (location_id,),
            )
            rows = cur.fetchall()
            cur.execute(
                """SELECT menu_item_id, alias FROM menu_item_aliases
                   WHERE menu_item_id IN (SELECT menu_item_id FROM menu_items WHERE location_id = %s)""",
                (location_id,),
            )
            alias_rows = cur.fetchall()
            cur.execute("SELECT currency_code FROM menu_settings WHERE location_id = %s", (location_id,))
            settings_row = cur.fetchone()
    except Exception as e:
        print(f"Menu load from DB failed: {e}")
        sys.exit(1)

    if not rows:
        print(f"No menu_items found for location_id={location_id} — run seed_db.py first.")
        sys.exit(1)

    if settings_row and settings_row[0]:
        CURRENCY_SYMBOL = _CURRENCY_SYMBOLS.get(settings_row[0], settings_row[0] + " ")

    aliases_by_item: Dict[str, List[str]] = {}
    for menu_item_id, alias in alias_rows:
        aliases_by_item.setdefault(str(menu_item_id), []).append(alias)

    items = []
    for menu_item_id, item_name, category_name, price, is_available, description in rows:
        items.append({
            "item_id": str(menu_item_id),
            "name": item_name,
            "category": (category_name or "all").strip().lower(),
            "price": float(price),
            "available": is_available,
            "aliases": aliases_by_item.get(str(menu_item_id), []),
            "description": description or "",
        })

    lookup = {}
    for item in items:
        lookup[item["name"].strip().lower()] = item
        for alias in item["aliases"]:
            lookup[alias.strip().lower()] = item

    MENU_ITEMS, MENU_LOOKUP = items, lookup
    print(f"[db] Loaded {len(items)} menu items from Postgres")


def find_item(spoken_name: Optional[str]) -> Optional[dict]:
    if not spoken_name:
        return None
    key = spoken_name.strip().lower()
    if key in MENU_LOOKUP:
        return MENU_LOOKUP[key]
    matches = difflib.get_close_matches(key, MENU_LOOKUP.keys(), n=1, cutoff=0.72)
    return MENU_LOOKUP[matches[0]] if matches else None


def get_price(item_name: Optional[str]) -> Optional[float]:
    item = find_item(item_name)
    return item["price"] if item else None


def canonical_name(item_name: Optional[str]) -> Optional[str]:
    """The menu's own spelling of a dish, given whatever the guest/LLM
    called it (fuzzy-matched) — use this to normalize order_items[n].name
    once an item is matched, so later price/total lookups hit directly."""
    item = find_item(item_name)
    return item["name"] if item else None


def get_categories() -> List[str]:
    return sorted({i["category"] for i in MENU_ITEMS if i["available"]})


def get_items_by_category(category: str) -> List[dict]:
    return [i for i in MENU_ITEMS if i["available"] and i["category"] == category]


def find_category(spoken_category: Optional[str]) -> Optional[str]:
    if not spoken_category:
        return None
    cats = get_categories()
    key = spoken_category.strip().lower()
    for c in cats:
        if c == key:
            return c
    matches = difflib.get_close_matches(key, cats, n=1, cutoff=0.6)
    return matches[0] if matches else None


def describe_category(category: str) -> str:
    items = get_items_by_category(category)
    if not items:
        return "nothing available right now"
    return "; ".join(
        f"{i['name']} ({CURRENCY_SYMBOL}{i['price']:.2f})" + (f" — {i['description']}" if i.get("description") else "")
        for i in items
    )
