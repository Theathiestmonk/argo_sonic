"""
menu_client.py
---------------
HTTP client for the shared, real menu (backend/launcher.py's GET /menu) — the
same file frontend/public/menu.html edits and syncs to. Without this, Sonic
has no way to know real stock/availability at all (see commands.py's
TAKE_ORDER docstring/history for why that matters).
"""

import re

import requests

import config

_cache: list[dict] | None = None


def get_menu(force_refresh: bool = False) -> list[dict]:
    """Session-cached — a table visit is short, no need to refetch every turn.
    Empty list (not an exception) on failure — a missing/unreachable backend
    just means no items can be matched, not a crash mid-conversation."""
    global _cache
    if _cache is not None and not force_refresh:
        return _cache
    try:
        resp = requests.get(f"{config.LAUNCHER_URL}/menu", timeout=3)
        resp.raise_for_status()
        _cache = resp.json().get("menu") or []
    except requests.RequestException as e:
        print(f"  [Menu] Could not reach backend for menu: {e}")
        _cache = []
    return _cache


def is_available(item: dict) -> bool:
    """Literal port of frontend/public/menu-data.js's isAvailable() rule —
    keep these two in lockstep: manual `available: false` always wins; a
    tracked numeric stock of 0 forces unavailable regardless of the toggle;
    a missing/non-numeric stock means untracked/unlimited."""
    if item.get("available") is False:
        return False
    stock = item.get("stock")
    if isinstance(stock, (int, float)) and stock <= 0:
        return False
    return True


_QTY_WORDS = {
    "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
}

# Words too generic to ever serve as a head-noun match target on their own
# in pass 2 — e.g. "chicken" happening to be the last word of "Butter
# Chicken" doesn't mean a customer saying just "chicken" wants that ONE
# specific dish out of the dozen chicken-containing items on the menu.
# These are protein/diet/filler words, handled instead as a browse-by-diet
# or general-suggestion cue (see main.py's _detect_diet_cue) — a customer
# can still order "Butter Chicken" by name, this only blocks the bare word.
_GENERIC_HEAD_NOUNS = {
    "chicken", "veg", "vegetarian", "food", "item", "items",
    "dish", "meal", "order", "menu", "something", "anything",
}


def _explicit_qty(text_lower: str, start: int, end: int) -> int | None:
    """Checks both sides of the matched item name — "two burgers" (qty word
    leads) and "burger 2" / "burger x2" (bare number trails) are both
    natural, real phrasings; leading wins if somehow both are present.
    Returns None (not a default of 1) when no quantity was actually spoken —
    callers that care about the difference between "no number said" and "the
    customer said one" (e.g. removal — see main.py's _handle_removal, where
    no number means "take the whole line off", not "remove exactly one")
    need that distinction; _extract_qty below is for callers that don't."""
    prefix = text_lower[max(0, start - 15):start]
    m = re.search(r"(\d+)\s*$", prefix)
    if m:
        return int(m.group(1))
    for word, val in _QTY_WORDS.items():
        if re.search(rf"\b{word}\b\s*$", prefix):
            return val

    suffix = text_lower[end:end + 8]
    m = re.search(r"^\s*x?\s*(\d+)\b", suffix)
    if m:
        return int(m.group(1))

    return None


def _extract_qty(text_lower: str, start: int, end: int) -> int:
    """Same as _explicit_qty, defaulting to 1 when nothing was said — what
    every caller that just wants an add-quantity (default: one of it) uses."""
    return _explicit_qty(text_lower, start, end) or 1


def category_alternatives(category: str | None, menu_items: list[dict],
                           exclude_ids: set = frozenset(), limit: int = 2) -> list[dict]:
    """Other available items in the same category — turns a flat rejection
    ("X isn't available") into something actually useful, and gives the
    customer real options when a head-noun match came back ambiguous."""
    if not category:
        return []
    out = []
    for item in menu_items:
        if item.get("category") != category:
            continue
        if item.get("id") in exclude_ids:
            continue
        if not is_available(item):
            continue
        out.append(item)
        if len(out) >= limit:
            break
    return out


def suggest_items(menu_items: list[dict], diet: str | None = None,
                   category: str | None = None, limit: int = 2) -> list[dict]:
    """Picks a few available items to recommend when a customer asks for a
    suggestion (generally, or scoped to a diet/protein they named — "what's
    good", "suggest something chicken", "any veg options"). Prefers items
    with a real signal of being worth suggesting — a non-empty eventLabel
    ("Best Seller", "Chef's Special", etc.) or an active discount — over an
    arbitrary first-N pick, falling back to whatever's available if nothing
    is specifically flagged."""
    candidates = [i for i in menu_items if is_available(i)]
    if diet:
        candidates = [i for i in candidates if (i.get("diet") or "").lower() == diet]
    if category:
        candidates = [i for i in candidates if i.get("category") == category]

    def score(i):
        return (1 if i.get("eventLabel") else 0) + (1 if i.get("discountActive") else 0)

    candidates.sort(key=score, reverse=True)
    return candidates[:limit]


def find_menu_items_in_text(text: str, menu_items: list[dict]):
    """
    Finds menu items mentioned in free text — in two passes, since real
    speech rarely uses a dish's full catalog name:

    Pass 1: the item's full name as a substring (longest name first, so e.g.
    "Double Chicken Cheese Burger" claims its span before a shorter
    also-matching name like "Chicken ... Burger" can steal part of it).

    Pass 2 (only for items pass 1 didn't already match): just the name's
    last word (its head noun — "milkshake" in "Chocolate Milkshake",
    "burger" in "Classic Chicken Burger") as a whole word — this is what
    catches a customer just saying "milkshake" or "one burger" without the
    full name. When exactly one menu item has that head noun, it's an
    unambiguous match. When several items share it (e.g. "Classic Chicken
    Burger" and "Veggie Burger" both end in "burger"), a bare "burger 3"
    does NOT guess one — that used to silently always pick the same one
    regardless of what the customer actually said, which is wrong, not
    just imprecise. Instead it requires one of the OTHER words in that
    candidate's name (e.g. "chicken" or "veggie") to also appear in the
    text; if exactly one candidate is disambiguated that way, it wins — if
    none or more than one are, the whole candidate group is surfaced as
    "ambiguous" instead of silently dropped, so the caller can read the
    options back ("We have a Classic Chicken Burger and a Veggie Burger —
    which would you like?") instead of a dead-end "didn't catch that."

    A leading quantity word/digit right before the match is captured
    (default 1 if none found — see "qty_explicit" below for telling that
    default apart from the customer actually saying "one").

    Returns (matched, ambiguous):
      matched   — [{"id","name","price","qty","qty_explicit","available",
                  "category"}, ...], availability already resolved so
                  callers never re-check it. "qty" defaults to 1 when no
                  quantity was said (right for adding — "burger" means one);
                  "qty_explicit" is None in that same case instead of 1,
                  for callers where "nothing said" and "said one" must be
                  told apart (e.g. removal, see main.py's _handle_removal).
      ambiguous — [[{"id","name","category"}, ...], ...] — one list per
                  head-noun group that matched but couldn't be told apart.
    """
    text_lower = text.lower()
    consumed = [False] * len(text_lower)
    found = []
    ambiguous = []
    matched_ids = set()

    ordered = sorted(menu_items, key=lambda i: len(i.get("name") or ""), reverse=True)

    # Pass 1 — full name
    for item in ordered:
        name = (item.get("name") or "").strip()
        if not name:
            continue
        name_lower = name.lower()
        idx = text_lower.find(name_lower)
        if idx == -1:
            continue
        span = range(idx, idx + len(name_lower))
        if any(consumed[i] for i in span):
            continue
        for i in span:
            consumed[i] = True
        matched_ids.add(item.get("id"))
        found.append({
            "id": item.get("id"),
            "name": name,
            "price": item.get("price", 0),
            "qty": _extract_qty(text_lower, idx, idx + len(name_lower)),
            "qty_explicit": _explicit_qty(text_lower, idx, idx + len(name_lower)),
            "available": is_available(item),
            "category": item.get("category"),
        })

    # Pass 2 — head noun (last word), for whatever pass 1 missed, grouped so
    # ambiguity between items sharing a head noun can actually be detected.
    by_head_noun: dict[str, list[dict]] = {}
    for item in ordered:
        if item.get("id") in matched_ids:
            continue
        name = (item.get("name") or "").strip()
        if not name:
            continue
        head_noun = name.split()[-1].lower()
        if head_noun in _GENERIC_HEAD_NOUNS:
            continue  # see _GENERIC_HEAD_NOUNS — too generic to match on alone
        by_head_noun.setdefault(head_noun, []).append(item)

    for head_noun, candidates in by_head_noun.items():
        # Optional trailing 's' — plain substring matching in pass 1 handles
        # plurals for free ("milkshake" is a substring of "milkshakes"), but
        # this regex-based pass needs it spelled out or "two milkshakes"
        # silently fails to match "Milkshake" at all.
        m = re.search(rf"\b{re.escape(head_noun)}s?\b", text_lower)
        if not m:
            continue
        idx, end = m.start(), m.end()
        span = range(idx, end)
        if any(consumed[i] for i in span):
            continue

        if len(candidates) == 1:
            chosen = candidates[0]
        else:
            disambiguated = [
                cand for cand in candidates
                if any(
                    len(w) > 2 and re.search(rf"\b{re.escape(w)}\b", text_lower)
                    for w in cand["name"].lower().split()[:-1]
                )
            ]
            if len(disambiguated) != 1:
                for i in span:
                    consumed[i] = True
                ambiguous.append([
                    {"id": c.get("id"), "name": c["name"], "category": c.get("category")}
                    for c in candidates
                ])
                continue
            chosen = disambiguated[0]

        for i in span:
            consumed[i] = True
        matched_ids.add(chosen.get("id"))
        found.append({
            "id": chosen.get("id"),
            "name": chosen["name"],
            "price": chosen.get("price", 0),
            "qty": _extract_qty(text_lower, idx, end),
            "qty_explicit": _explicit_qty(text_lower, idx, end),
            "available": is_available(chosen),
            "category": chosen.get("category"),
        })

    return found, ambiguous
