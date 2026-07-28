"""
order_cart.py
-------------
Structured cart for an in-progress voice order. Replaces dispatcher.py's old
_order_queue (a flat list of opaque free-text strings) with real
{id, name, qty, price} lines and a real running total.
"""


class OrderCart:
    def __init__(self):
        self.items: list[dict] = []  # [{id, name, qty, price}]

    def add(self, item_id, name: str, qty: int, price: float):
        for line in self.items:
            if line["id"] == item_id:
                line["qty"] += qty
                return
        self.items.append({"id": item_id, "name": name, "qty": qty, "price": price})

    def remove(self, item_id, qty: int | None = None) -> bool:
        """qty=None (or >= the line's current qty) removes the whole line —
        "remove the burger" when there's only one on the order. A smaller
        qty decrements instead — "remove 2 mutton biryani" out of 3 leaves
        1, rather than dropping all 3 (that used to happen unconditionally,
        which is wrong when the customer only over-ordered by a couple).
        Returns whether it was actually there."""
        for i, line in enumerate(self.items):
            if line["id"] == item_id:
                if qty is None or qty >= line["qty"]:
                    del self.items[i]
                else:
                    line["qty"] -= qty
                return True
        return False

    def total(self) -> float:
        return round(sum(line["qty"] * line["price"] for line in self.items), 2)

    def to_dict(self) -> dict:
        return {"items": list(self.items), "total": self.total()}

    def is_empty(self) -> bool:
        return not self.items

    def clear(self):
        self.items = []
