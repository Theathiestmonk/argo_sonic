"""In-memory knowledge: waypoints, menu (simple lexical RAG), restaurant facts.

Everything is loaded once and kept hot; the menu refreshes in the background
(stale-while-revalidate) so a turn never waits on disk or HTTP.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Optional

import httpx

from .config import Settings

log = logging.getLogger("nlp.knowledge")

_NUM_WORDS = {
    "zero": "0", "one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6",
    "seven": "7", "eight": "8", "nine": "9", "ten": "10", "eleven": "11", "twelve": "12",
}
_STOP = {"the", "a", "an", "to", "please", "my", "your", "of", "some", "any", "me", "number", "no", "at", "go", "near"}


def norm(s: str) -> str:
    s = (s or "").lower()
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    toks = [_NUM_WORDS.get(t, t) for t in s.split()]
    return " ".join(toks)


def _stem(t: str) -> str:
    for suf in ("ies", "es", "s"):
        if t.endswith(suf) and len(t) > len(suf) + 2:
            return t[: -len(suf)] + ("y" if suf == "ies" else "")
    return t


def tokens(s: str) -> list[str]:
    return [_stem(t) for t in norm(s).split() if t not in _STOP]


def similarity(a: str, b: str) -> float:
    """0..1. Blends char-level ratio with token containment so 'chicken burger'
    finds 'Classic Chicken Burger' and 'table three' finds 'Table 3'."""
    na, nb = norm(a), norm(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    ratio = SequenceMatcher(None, na, nb).ratio()
    ta, tb = set(tokens(a)), set(tokens(b))
    if ta and tb:
        # word-level fuzzy match so "biriyani" still finds "Biryani"
        hit = lambda t, pool: any(t == u or SequenceMatcher(None, t, u).ratio() >= 0.8 for u in pool)
        ia = sum(hit(t, tb) for t in ta)                       # query words found in the name
        ib = sum(hit(u, ta) for u in tb)                       # name words found in the query
        if ia == len(ta) or ib == len(tb):
            ratio = max(ratio, 0.80 + 0.15 * max(ia, ib) / max(len(ta), len(tb)))
        else:
            ratio = max(ratio, 0.6 * max(ia, ib) / max(len(ta), len(tb)))
    return ratio


def best_match(query: str, candidates: list[str], k: int = 1) -> list[tuple[str, float]]:
    scored = sorted(((c, similarity(query, c)) for c in candidates), key=lambda x: -x[1])
    return scored[:k]


# ───────────────────────────── Waypoints ─────────────────────────────

class Waypoints:
    """Named waypoints of the map that navigation is *currently running*.

    The running map comes from the dashboard launcher (GET /status → {"running", "map"}), polled every
    few seconds in a background thread; when it changes, that map's <waypoints_dir>/<map>.json is loaded.
    Falls back to MAP_NAME when the launcher is unreachable or nothing is running. The file itself is
    re-read when it changes (the dashboard edits it live) – a stat() per lookup, no parsing otherwise."""

    ACCEPT = 0.82      # confident match -> just go
    SUGGEST = 0.45     # weaker match -> "did you mean ... ?"
    POLL_S = 3.0

    def __init__(self, s: Settings, aliases: Optional[dict] = None, follow_launcher: Optional[bool] = None):
        self.s = s
        self.dir = s.waypoints_dir
        self.map_name = s.map_name
        self.aliases = {norm(k): v for k, v in (aliases or {}).items()}   # "base" -> "Docker"
        self._mtime = 0.0
        self._path_loaded = None
        self._wps: dict[str, dict] = {}   # name -> waypoint dict
        self._follow = s.map_from_launcher if follow_launcher is None else follow_launcher
        if self._follow:
            self._poll_once(timeout=5.0)   # launcher can be slow right after boot
            threading.Thread(target=self._poll_loop, daemon=True, name="map-poll").start()
        self._load()

    # -- which map is navigation running? ------------------------------
    def _poll_once(self, timeout: float = 0.8) -> None:
        try:
            d = httpx.get(self.s.launcher_status_url, timeout=timeout).json()
            m = d.get("map")
            if d.get("running") and m and m != self.map_name:
                log.info("navigation map is now %r (was %r)", m, self.map_name)
                self.map_name = m
        except Exception:
            pass        # launcher down → keep the last known map

    def _poll_loop(self) -> None:
        while True:
            time.sleep(self.POLL_S)
            self._poll_once()

    @property
    def path(self):
        return self.dir / f"{self.map_name}.json"

    def _load(self) -> None:
        path = self.path
        try:
            m = path.stat().st_mtime
            if m == self._mtime and path == self._path_loaded:
                return
            raw = json.loads(path.read_text(encoding="utf-8"))
            self._wps = {v["name"].strip(): v for v in raw.values() if isinstance(v, dict) and v.get("name")}
            self._mtime, self._path_loaded = m, path
            log.info("waypoints [%s]: %s", self.map_name, ", ".join(self._wps))
        except Exception as e:  # keep last good copy
            if path != self._path_loaded:
                self._wps, self._path_loaded = {}, path      # new map without a waypoints file → no stale places
            log.warning("waypoints load failed (%s): %s", path, e)

    @property
    def names(self) -> list[str]:
        self._load()
        return list(self._wps)

    def table_numbers(self) -> set[int]:
        out = set()
        for n in self.names:
            m = re.fullmatch(r"table\s*(\d+)", n.strip(), re.I)
            if m:
                out.add(int(m.group(1)))
        return out

    def get(self, name: str) -> Optional[dict]:
        self._load()
        return self._wps.get(name)

    def resolve(self, query: str) -> tuple[Optional[str], Optional[str]]:
        """-> (exact_name, suggestion). Exactly one of them is set, or both None."""
        names = self.names
        if not query or not names:
            return None, None
        tgt = self.aliases.get(norm(query))
        if tgt and tgt in self._wps:
            return tgt, None
        (name, score), = best_match(query, names, 1)
        if score >= self.ACCEPT:
            # "table 9" must never silently become "Table 5": numbers have to match exactly.
            if set(re.findall(r"\d+", norm(query))) == set(re.findall(r"\d+", norm(name))):
                return name, None
            return None, name
        if score >= self.SUGGEST:
            return None, name
        return None, None


# ───────────────────────────── Menu ─────────────────────────────

@dataclass
class MenuItem:
    id: str
    name: str
    category: str
    price: float
    desc: str = ""
    diet: str = ""            # veg | nonveg
    available: bool = True
    discount: int = 0
    label: str = ""           # eventLabel: "Chef Special", "Best Seller" ...
    variation: Optional[dict] = None   # {"label": "size", "options": [...]}
    _blob: str = field(default="", repr=False)

    @property
    def sellable(self) -> bool:
        return self.available

    def offer_price(self) -> float:
        return round(self.price * (1 - self.discount / 100), 2) if self.discount else self.price


class Menu:
    FAMOUS_LABELS = {"best seller", "popular", "chef special", "street favorite", "street favourite"}

    def __init__(self, s: Settings, restaurant: "Restaurant"):
        self.s = s
        self.r = restaurant
        self.items: list[MenuItem] = []
        self.currency = "USD"
        self._lock = threading.Lock()
        self._loaded_at = 0.0
        self._refreshing = False
        self.reload_sync()

    # -- loading -------------------------------------------------------
    def _fetch(self) -> dict:
        try:
            r = httpx.get(self.s.menu_url, timeout=6.0)   # DB-backed, ~2 s; only ever awaited at boot or in a bg thread
            r.raise_for_status()
            d = r.json()
            if d.get("menu"):
                return d
        except Exception:
            pass
        return json.loads(self.s.menu_file.read_text(encoding="utf-8"))

    def reload_sync(self) -> None:
        try:
            d = self._fetch()
        except Exception as e:
            log.warning("menu load failed: %s", e)
            return
        items = []
        var = self.r.data.get("variations", {})
        for m in d.get("menu", []):
            cat = m.get("category") or "Other"
            v = (var.get("by_item") or {}).get(m["name"], "missing")
            if v == "missing":
                v = (var.get("by_category") or {}).get(cat)
            it = MenuItem(
                id=str(m["id"]), name=m["name"], category=cat, price=float(m.get("price") or 0),
                desc=m.get("desc") or "", diet=m.get("diet") or "",
                available=bool(m.get("available", True)) and (m.get("stock") is None or m.get("stock", 1) > 0),
                discount=int(m.get("discountPercent") or 0) if m.get("discountActive") else 0,
                label=m.get("eventLabel") or "", variation=v,
            )
            it._blob = " ".join([it.name, it.category, it.desc, it.label, "vegetarian veg" if it.diet == "veg" else "non-veg nonveg meat"])
            items.append(it)
        with self._lock:
            self.items = items
            self.currency = (d.get("settings") or {}).get("currencyCode", self.currency)
            self._loaded_at = time.time()
        log.info("menu: %d items (%d available)", len(items), sum(i.available for i in items))

    def maybe_refresh(self, ttl: float = 60.0) -> None:
        """Non-blocking: kicks a background reload when stale."""
        if self._refreshing or time.time() - self._loaded_at < ttl:
            return
        self._refreshing = True

        def run():
            try:
                self.reload_sync()
            finally:
                self._refreshing = False
        threading.Thread(target=run, daemon=True).start()

    # -- queries -------------------------------------------------------
    @property
    def categories(self) -> list[str]:
        seen: dict[str, None] = {}
        for i in self.items:
            if i.available:
                seen.setdefault(i.category, None)
        return list(seen)

    def in_category(self, cat: str, diet: Optional[str] = None, only_available: bool = True) -> list[MenuItem]:
        return [i for i in self.items if i.category == cat and (i.available or not only_available) and (not diet or i.diet == diet)]

    def resolve_categories(self, q: str) -> list[str]:
        """'burgers' -> ['Burger'], 'snacks' -> ['Starter','Sides','Wrap'] (aliases from restaurant.json)."""
        if not q:
            return []
        aliases = self.r.data.get("category_aliases", {})
        nq = norm(q)
        for k, cats in aliases.items():
            if norm(k) == nq or (_stem(nq) == _stem(norm(k))):
                have = set(self.categories)
                return [c for c in cats if c in have]
        cats = self.categories
        (c, sc), = best_match(q, cats, 1) if cats else [("", 0.0)]
        return [c] if sc >= 0.75 else []

    def find_item(self, q: str, threshold: float = 0.8) -> tuple[Optional[MenuItem], Optional[MenuItem]]:
        """-> (confident, suggestion). Includes unavailable items so we can say so."""
        if not q or not self.items:
            return None, None
        scored = sorted(((i, similarity(q, i.name)) for i in self.items), key=lambda x: -x[1])
        top, sc = scored[0]
        if sc >= threshold:
            return top, None
        # A weak character-level match alone is how "cheese fries" became "Jeera Rice" – a suggestion must either
        # be quite close, or share a real word (stem) with the dish name.
        shares_word = bool(set(tokens(q)) & set(tokens(top.name)))
        if sc >= 0.72 or (sc >= 0.5 and shares_word):
            return None, top
        return None, None

    def ambiguous(self, q: str, limit: int = 4) -> list[MenuItem]:
        """Several dishes fit equally well ("kebab", "naan", "biryani") → the options, so the robot can ask which one."""
        if not q:
            return []
        scored = sorted(((i, similarity(q, i.name)) for i in self.items if i.available), key=lambda x: -x[1])
        if not scored or scored[0][1] >= 0.999 or scored[0][1] < 0.8:
            return []
        close = [i for i, sc in scored if sc >= 0.8 and scored[0][1] - sc < 0.06]
        return close[:limit] if len(close) >= 2 else []

    def specials(self, n: int = 5) -> list[MenuItem]:
        manual = [self.find_item(x)[0] for x in self.r.data.get("specials_today", [])]
        manual = [m for m in manual if m and m.available]
        if manual:
            return manual[:n]
        deals = [i for i in self.items if i.available and i.discount]
        deals.sort(key=lambda i: (-i.discount, i.name))
        return deals[:n]

    def famous(self, n: int = 5) -> list[MenuItem]:
        manual = [self.find_item(x)[0] for x in self.r.data.get("famous_items", [])]
        manual = [m for m in manual if m and m.available]
        if manual:
            return manual[:n]
        return [i for i in self.items if i.available and i.label.lower() in self.FAMOUS_LABELS][:n]

    def search(self, q: str, k: int = 6, diet: Optional[str] = None) -> list[MenuItem]:
        """Tiny lexical RAG: token overlap over name+category+desc+label, name hits weighted up."""
        qt = set(tokens(q))
        if not qt:
            return []
        out = []
        for i in self.items:
            if not i.available or (diet and i.diet != diet):
                continue
            nt, bt = set(tokens(i.name)), set(tokens(i._blob))
            score = 2.0 * len(qt & nt) + 1.0 * len(qt & bt) + 0.5 * max(similarity(q, i.name) - 0.5, 0)
            if score > 0:
                out.append((score, i))
        out.sort(key=lambda x: -x[0])
        return [i for _, i in out[:k]]

    def money(self, v: float) -> str:
        unit = {"INR": "rupees", "USD": "dollars", "EUR": "euros", "GBP": "pounds", "AED": "dirhams"}.get(self.currency, self.currency)
        return f"{v:g} {unit}"

    def compact_for_prompt(self) -> str:
        return "; ".join(f"{i.name} ({i.category})" for i in self.items if i.available)

    def rag_context(self, items: list[MenuItem]) -> str:
        return "\n".join(
            f"- {i.name} [{i.category}, {'veg' if i.diet == 'veg' else 'non-veg'}] {self.money(i.offer_price())}"
            f"{' (deal: ' + str(i.discount) + '% off)' if i.discount else ''}: {i.desc}" for i in items)


# ───────────────────────────── Restaurant ─────────────────────────────

class Restaurant:
    def __init__(self, s: Settings):
        self.data: dict = json.loads(s.restaurant_file.read_text(encoding="utf-8"))
        self.bot_name = self.data.get("bot_name", "Sonic")
        self.name = self.data.get("name", "our restaurant")

    def facts(self) -> str:
        d = self.data
        parts = [
            f"Restaurant: {d.get('name')} – {d.get('tagline', '')}",
            f"About: {d.get('about', '')}",
            f"Cuisine: {', '.join(d.get('cuisine', []))}",
            f"Hours: {d.get('hours', '')}",
            f"Address: {d.get('address', '')}",
            f"Facilities: {', '.join(d.get('facilities', []))}",
            f"Payment: {d.get('payment', '')}",
            f"Policies: {d.get('policies', '')}",
        ]
        return "\n".join(p for p in parts if p.split(': ', 1)[-1].strip())

    def service_item(self, q: str) -> Optional[str]:
        nq = norm(q)
        best, bs = None, 0.0
        for si in self.data.get("service_items", []):
            for alias in [si["name"], *si.get("aliases", [])]:
                sc = 1.0 if norm(alias) == nq else similarity(q, alias)
                if sc > bs:
                    best, bs = si["name"], sc
        return best if bs >= 0.85 else None
