"""
Menu database with fuzzy matching for item validation.

Handles: exact match, fuzzy match (typos/partial names), not-found suggestions.
"""

import json
import difflib
import os
from typing import List, Optional, Dict, Any

_DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")


class MenuDatabase:
    def __init__(self, menu_path: str = ""):
        path = menu_path or os.path.join(_DATA_DIR, "menu.json")
        with open(path) as f:
            data = json.load(f)
        self.items: List[Dict] = data["menu"]
        self.currency: str = data.get("currency", "INR")
        # Build lookup maps
        self._by_id   = {i["id"]: i for i in self.items}
        self._by_name = {i["name"].lower(): i for i in self.items}
        # Flat list of all searchable strings → item
        self._search_index: List[tuple] = []
        for item in self.items:
            self._search_index.append((item["name"].lower(), item))
            for tag in item.get("tags", []):
                self._search_index.append((tag.lower(), item))

    # ── Public API ────────────────────────────────────────────────────────────

    def find_item(self, query: str, threshold: float = 0.6) -> Dict[str, Any]:
        """Find a menu item by name with fuzzy fallback.

        Returns:
            {
                "exact": item or None,
                "fuzzy": item or None,
                "fuzzy_score": float,
                "suggestions": [item, ...]  # top 3 alternatives
            }
        """
        q = query.lower().strip()

        # 1. Exact name match
        if q in self._by_name:
            return {"exact": self._by_name[q], "fuzzy": None, "fuzzy_score": 1.0, "suggestions": []}

        # 2. Substring match (e.g. "pizza" → "Paneer Pizza", "Margherita Pizza")
        substring_hits = [item for name, item in self._search_index if q in name]
        if len(substring_hits) == 1:
            return {"exact": substring_hits[0], "fuzzy": None, "fuzzy_score": 1.0, "suggestions": []}

        # 3. Fuzzy match on all searchable strings
        all_strings = [name for name, _ in self._search_index]
        close = difflib.get_close_matches(q, all_strings, n=3, cutoff=threshold)
        if close:
            best = close[0]
            best_item = next(item for name, item in self._search_index if name == best)
            score = difflib.SequenceMatcher(None, q, best).ratio()
            suggestions = []
            for s in close[1:]:
                si = next((item for name, item in self._search_index if name == s), None)
                if si and si not in suggestions:
                    suggestions.append(si)
            return {"exact": None, "fuzzy": best_item, "fuzzy_score": score, "suggestions": suggestions}

        # 4. Not found — return popular items as suggestions
        popular = self.items[:3]
        return {"exact": None, "fuzzy": None, "fuzzy_score": 0.0, "suggestions": popular}

    def get_by_category(self, category: str) -> List[Dict]:
        return [i for i in self.items if i["category"] == category]

    def format_menu_summary(self) -> str:
        """Short spoken summary of categories."""
        cats: Dict[str, List[str]] = {}
        for item in self.items:
            cats.setdefault(item["category"], []).append(item["name"])
        parts = []
        for cat, names in cats.items():
            parts.append(f"{cat.title()}: {', '.join(names[:3])}" +
                         (f" and {len(names)-3} more" if len(names) > 3 else ""))
        return ". ".join(parts)

    def calculate_total(self, order_items: List[Dict]) -> float:
        return sum(i["unit_price"] * i["quantity"] for i in order_items)

    def format_order_summary(self, order_items: List[Dict]) -> str:
        lines = [f"{i['quantity']} × {i['name']} @ ₹{i['unit_price']:.0f}" for i in order_items]
        total = self.calculate_total(order_items)
        return ", ".join(lines) + f". Total: ₹{total:.0f}"
