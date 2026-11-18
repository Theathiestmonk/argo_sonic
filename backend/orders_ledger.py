"""Order ledger – the POS system of record for orders (SQLite, stdlib only).

Design follows common POS practice:
  * every order has a short, unique, ever-increasing number (#1001, #1002 …) – what staff and guests say out loud
  * items are *snapshots* (name, list price, price charged) so later menu edits never rewrite history
  * money is computed server-side only: subtotal (offer prices) + tax = total, tax % frozen on the order
  * fulfilment status and payment status are independent
  * strict status lifecycle (forward moves, one step back to correct a mistake, cancel with a reason)
  * append-only audit trail (order_events): who changed what, when
  * optimistic concurrency (`version`) so two screens can't silently overwrite each other
  * orders are never deleted – cancelled / delivered / refunded are terminal states kept for history & reports
"""
from __future__ import annotations

import csv
import io
import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

FLOW = ["placed", "confirmed", "preparing", "ready", "out_for_delivery", "delivered"]
ACTIVE = ("placed", "confirmed", "preparing", "ready", "out_for_delivery")
TERMINAL = ("delivered", "cancelled", "refunded")
STATUSES = FLOW + ["cancelled", "refunded"]
SOURCES = ("voice_assistant", "pos", "staff", "mobile", "dine_in")
PAYMENT_STATUS = ("unpaid", "paid", "refunded")
PAYMENT_METHODS = ("cash", "card", "upi", "other")
EDITABLE = ("placed", "confirmed")            # items may change only before the kitchen starts


class OrderError(ValueError):
    """Raised for anything the client did wrong; `code` is the HTTP status to use."""
    def __init__(self, msg: str, code: int = 400):
        super().__init__(msg)
        self.code = code


def now_iso() -> str:
    n = datetime.now(timezone.utc)
    return n.strftime("%Y-%m-%dT%H:%M:%S.") + f"{n.microsecond // 1000:03d}Z"


def _r2(x: float) -> float:
    return round(float(x) + 1e-9, 2)


SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  map TEXT NOT NULL DEFAULT '',
  table_no TEXT NOT NULL DEFAULT '',
  source TEXT NOT NULL DEFAULT 'pos',
  status TEXT NOT NULL DEFAULT 'placed',
  payment_status TEXT NOT NULL DEFAULT 'unpaid',
  payment_method TEXT,
  subtotal REAL NOT NULL DEFAULT 0, tax_percent REAL NOT NULL DEFAULT 0, tax REAL NOT NULL DEFAULT 0, total REAL NOT NULL DEFAULT 0,
  currency TEXT NOT NULL DEFAULT 'INR',
  notes TEXT NOT NULL DEFAULT '', cancel_reason TEXT, customer TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL, paid_at TEXT,
  version INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS order_items (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  order_id INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
  menu_id TEXT, name TEXT NOT NULL, qty INTEGER NOT NULL, list_price REAL NOT NULL DEFAULT 0, unit_price REAL NOT NULL DEFAULT 0,
  notes TEXT NOT NULL DEFAULT '', kind TEXT NOT NULL DEFAULT 'menu'
);
CREATE TABLE IF NOT EXISTS order_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  order_id INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
  at TEXT NOT NULL, kind TEXT NOT NULL, from_status TEXT, to_status TEXT, actor TEXT NOT NULL DEFAULT 'staff', note TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS idx_orders_created ON orders(created_at);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
CREATE INDEX IF NOT EXISTS idx_orders_table ON orders(table_no);
CREATE INDEX IF NOT EXISTS idx_items_order ON order_items(order_id);
CREATE INDEX IF NOT EXISTS idx_events_order ON order_events(order_id);
"""


class Ledger:
    def __init__(self, path, menu_reader: Optional[Callable[[], dict]] = None):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self.menu_reader = menu_reader or (lambda: {"menu": [], "settings": {}})
        with self.lock:
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA foreign_keys=ON")
            self.db.executescript(SCHEMA)
            self.db.execute("INSERT OR IGNORE INTO meta(key, value) VALUES('rev', 0)")
            # numbering starts at #1001
            if not self.db.execute("SELECT 1 FROM sqlite_sequence WHERE name='orders'").fetchone():
                self.db.execute("INSERT INTO sqlite_sequence(name, seq) VALUES('orders', 1000)")

    # ───────────────────────── helpers ─────────────────────────
    def rev(self) -> int:
        return self.db.execute("SELECT value FROM meta WHERE key='rev'").fetchone()[0]

    def _bump(self) -> None:
        self.db.execute("UPDATE meta SET value = value + 1 WHERE key='rev'")

    def _event(self, oid, kind, frm=None, to=None, actor="staff", note=""):
        self.db.execute("INSERT INTO order_events(order_id, at, kind, from_status, to_status, actor, note) VALUES(?,?,?,?,?,?,?)",
                        (oid, now_iso(), kind, frm, to, actor or "staff", note or ""))

    def _menu_index(self):
        d = self.menu_reader() or {}
        by_id, by_name = {}, {}
        for m in d.get("menu", []):
            by_id[str(m.get("id"))] = m
            by_name[str(m.get("name", "")).strip().lower()] = m
        return by_id, by_name, d.get("settings") or {}

    @staticmethod
    def _offer_price(m) -> float:
        p = float(m.get("price") or 0)
        return _r2(p * (1 - (m.get("discountPercent") or 0) / 100)) if m.get("discountActive") and m.get("discountPercent") else p

    def _clean_items(self, items) -> list[dict]:
        if not isinstance(items, list) or not items:
            raise OrderError("an order needs at least one item")
        by_id, by_name, _ = self._menu_index()
        out = []
        for n, it in enumerate(items, 1):
            if not isinstance(it, dict):
                raise OrderError(f"item {n} is invalid")
            name = str(it.get("name", "")).strip()
            if not name:
                raise OrderError(f"item {n} has no name")
            try:
                qty = int(it.get("qty", it.get("quantity", 1)))
            except (TypeError, ValueError):
                raise OrderError(f"'{name}': quantity must be a whole number")
            if not 1 <= qty <= 99:
                raise OrderError(f"'{name}': quantity must be 1 – 99")
            m = by_id.get(str(it.get("menu_id") or it.get("id") or "")) or by_name.get(name.lower())
            if it.get("price") is not None:
                try:
                    unit = _r2(float(it["price"]))
                except (TypeError, ValueError):
                    raise OrderError(f"'{name}': price must be a number")
                if unit < 0:
                    raise OrderError(f"'{name}': price cannot be negative")
                lst = _r2(float(it.get("list_price", m.get("price") if m else unit) or unit))
            elif m:
                unit, lst = self._offer_price(m), _r2(m.get("price") or 0)
            else:
                unit = lst = 0.0                                  # free extras: water, napkins …
            out.append({"menu_id": str((m or {}).get("id") or it.get("menu_id") or ""), "name": (m or {}).get("name", name) if m else name,
                        "qty": qty, "list_price": lst, "unit_price": unit, "notes": str(it.get("notes") or it.get("variation") or "").strip()[:120],
                        "kind": "menu" if m else str(it.get("kind") or "service")})
        return out

    def _price(self, items, tax_percent) -> tuple[float, float, float]:
        sub = _r2(sum(i["unit_price"] * i["qty"] for i in items))
        tax = _r2(sub * tax_percent / 100)
        return sub, tax, _r2(sub + tax)

    # ───────────────────────── create ─────────────────────────
    def create(self, p: dict, actor: str = "staff") -> dict:
        items = self._clean_items(p.get("items"))
        source = p.get("source") or "pos"
        if source not in SOURCES:
            raise OrderError(f"source must be one of {', '.join(SOURCES)}")
        table = str(p.get("table", p.get("table_no", "")) or "").strip()[:20]
        _, _, settings = self._menu_index()
        tax_pct = float(settings.get("taxPercent", 0) or 0)
        sub, tax, total = self._price(items, tax_pct)
        t = now_iso()
        with self.lock:
            self.db.execute("BEGIN")
            try:
                cur = self.db.execute(
                    "INSERT INTO orders(map, table_no, source, status, subtotal, tax_percent, tax, total, currency, notes, customer, created_at, updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (str(p.get("map") or ""), table, source, "placed", sub, tax_pct, tax, total, settings.get("currencyCode", "INR"),
                     str(p.get("notes") or "").strip()[:300], str(p.get("customer") or "").strip()[:80], t, t))
                oid = cur.lastrowid
                self.db.executemany("INSERT INTO order_items(order_id, menu_id, name, qty, list_price, unit_price, notes, kind) VALUES(?,?,?,?,?,?,?,?)",
                                    [(oid, i["menu_id"], i["name"], i["qty"], i["list_price"], i["unit_price"], i["notes"], i["kind"]) for i in items])
                self._event(oid, "created", None, "placed", actor, f"via {source}")
                self._bump()
                self.db.execute("COMMIT")
            except Exception:
                self.db.execute("ROLLBACK")
                raise
        return self.get(oid)

    # ───────────────────────── read ─────────────────────────
    def _hydrate(self, rows, with_events=False) -> list[dict]:
        out = []
        for r in rows:
            d = dict(r)
            d["number"] = d["id"]
            d["items"] = [dict(i) for i in self.db.execute("SELECT * FROM order_items WHERE order_id=? ORDER BY id", (d["id"],))]
            if with_events:
                d["events"] = [dict(e) for e in self.db.execute("SELECT * FROM order_events WHERE order_id=? ORDER BY id", (d["id"],))]
            out.append(d)
        return out

    def get(self, oid: int) -> dict:
        with self.lock:
            r = self.db.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
            if not r:
                raise OrderError("order not found", 404)
            return self._hydrate([r], with_events=True)[0]

    def _where(self, f: dict, skip_status=False):
        w, a = [], []
        if not skip_status and f.get("status"):
            s = str(f["status"])
            if s == "active":
                w.append(f"status IN ({','.join('?' * len(ACTIVE))})"); a += ACTIVE
            elif s == "processed":
                w.append(f"status IN ({','.join('?' * len(TERMINAL))})"); a += TERMINAL
            else:
                parts = [x for x in s.split(",") if x in STATUSES]
                if parts:
                    w.append(f"status IN ({','.join('?' * len(parts))})"); a += parts
        if f.get("from"): w.append("created_at >= ?"); a.append(f["from"])
        if f.get("to"): w.append("created_at < ?"); a.append(f["to"])
        if f.get("table"): w.append("table_no = ?"); a.append(str(f["table"]))
        if f.get("source"): w.append("source = ?"); a.append(f["source"])
        if f.get("payment"): w.append("payment_status = ?"); a.append(f["payment"])
        q = str(f.get("q") or "").strip().lstrip("#")
        if q:
            like = f"%{q.lower()}%"
            w.append("(CAST(id AS TEXT) = ? OR lower(table_no) LIKE ? OR lower(notes) LIKE ? OR lower(customer) LIKE ? "
                     "OR id IN (SELECT order_id FROM order_items WHERE lower(name) LIKE ?))")
            a += [q, like, like, like, like]
        return (" WHERE " + " AND ".join(w)) if w else "", a

    def list(self, f: dict) -> dict:
        limit = max(1, min(int(f.get("limit") or 50), 500)); offset = max(0, int(f.get("offset") or 0))
        with self.lock:
            where, a = self._where(f)
            total = self.db.execute(f"SELECT COUNT(*) FROM orders{where}", a).fetchone()[0]
            order = "ASC" if f.get("sort") == "oldest" else "DESC"
            rows = self.db.execute(f"SELECT * FROM orders{where} ORDER BY created_at {order}, id {order} LIMIT ? OFFSET ?", a + [limit, offset]).fetchall()
            w2, a2 = self._where(f, skip_status=True)
            counts = {r[0]: r[1] for r in self.db.execute(f"SELECT status, COUNT(*) FROM orders{w2} GROUP BY status", a2)}
            return {"orders": self._hydrate(rows), "total": total, "counts": counts, "rev": self.rev()}

    def summary(self, f: dict) -> dict:
        with self.lock:
            where, a = self._where({**f, "status": None})
            rows = self.db.execute(f"SELECT status, payment_status, total, created_at, id FROM orders{where}", a).fetchall()
            live = [r for r in rows if r["status"] != "cancelled" and r["status"] != "refunded"]
            prep = []
            for r in live:
                ev = {e["to_status"]: e["at"] for e in self.db.execute("SELECT to_status, at FROM order_events WHERE order_id=? AND kind='status'", (r["id"],))}
                if "ready" in ev:
                    prep.append((_ts(ev["ready"]) - _ts(r["created_at"])) / 60)
            return {"orders": len(live), "sales": _r2(sum(r["total"] for r in live)),
                    "paid": _r2(sum(r["total"] for r in live if r["payment_status"] == "paid")),
                    "open": sum(1 for r in rows if r["status"] in ACTIVE), "open_value": _r2(sum(r["total"] for r in rows if r["status"] in ACTIVE)),
                    "cancelled": sum(1 for r in rows if r["status"] == "cancelled"),
                    "avg_prep_min": round(sum(prep) / len(prep), 1) if prep else None, "rev": self.rev()}

    def by_table(self) -> dict:
        """Dashboard-compatible view: {table: {items, total, status, updatedAt, ids}} for every open order."""
        with self.lock:
            rows = self.db.execute(f"SELECT * FROM orders WHERE status IN ({','.join('?' * len(ACTIVE))}) AND table_no != '' ORDER BY created_at", ACTIVE).fetchall()
            out: dict = {}
            for o in self._hydrate(rows):
                e = out.setdefault(o["table_no"], {"items": [], "total": 0.0, "status": o["status"], "updatedAt": o["updated_at"], "ids": []})
                e["items"] += [{"id": i["menu_id"] or i["name"], "name": i["name"] + (f" ({i['notes']})" if i["notes"] else ""), "qty": i["qty"], "price": i["unit_price"]} for i in o["items"]]
                e["total"] = _r2(e["total"] + o["total"]); e["status"] = o["status"]; e["updatedAt"] = o["updated_at"]; e["ids"].append(o["id"])
            return out

    # ───────────────────────── update ─────────────────────────
    def _allowed(self, cur: str, to: str) -> bool:
        if cur == to:
            return False
        if to == "cancelled":
            return cur in ACTIVE
        if to == "refunded":
            return cur == "delivered"
        if cur in TERMINAL:
            return False
        i, j = FLOW.index(cur), FLOW.index(to) if to in FLOW else -1
        return j > i or j == i - 1            # forward (skipping allowed) or exactly one step back to fix a mistake

    def patch(self, oid: int, p: dict, actor: str = "staff") -> dict:
        with self.lock:
            r = self.db.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
            if not r:
                raise OrderError("order not found", 404)
            if p.get("version") is not None and int(p["version"]) != r["version"]:
                raise OrderError("This order was changed on another screen – reloaded the latest.", 409)
            cur, sets, params = r["status"], [], []
            self.db.execute("BEGIN")
            try:
                if "items" in p:
                    if cur not in EDITABLE:
                        raise OrderError("Items can only be changed before the kitchen starts preparing", 409)
                    items = self._clean_items(p["items"])
                    sub, tax, total = self._price(items, r["tax_percent"])
                    self.db.execute("DELETE FROM order_items WHERE order_id=?", (oid,))
                    self.db.executemany("INSERT INTO order_items(order_id, menu_id, name, qty, list_price, unit_price, notes, kind) VALUES(?,?,?,?,?,?,?,?)",
                                        [(oid, i["menu_id"], i["name"], i["qty"], i["list_price"], i["unit_price"], i["notes"], i["kind"]) for i in items])
                    sets += ["subtotal=?", "tax=?", "total=?"]; params += [sub, tax, total]
                    self._event(oid, "edit", actor=actor, note=f"items updated ({sum(i['qty'] for i in items)} items)")
                for fld, col, maxlen in (("notes", "notes", 300), ("customer", "customer", 80), ("table", "table_no", 20)):
                    if fld in p:
                        if cur in TERMINAL:
                            raise OrderError("This order is closed", 409)
                        v = str(p[fld] or "").strip()[:maxlen]
                        if v != r[col]:
                            sets.append(f"{col}=?"); params.append(v)
                            self._event(oid, "edit", actor=actor, note=f"{fld}: {v or '—'}")
                if p.get("status"):
                    to = p["status"]
                    if to not in STATUSES:
                        raise OrderError("unknown status")
                    if not self._allowed(cur, to):
                        raise OrderError(f"Can't move an order from {cur.replace('_', ' ')} to {to.replace('_', ' ')}", 409)
                    reason = str(p.get("reason") or "").strip()[:200]
                    if to == "cancelled" and not reason:
                        raise OrderError("Choose a reason for cancelling")
                    sets.append("status=?"); params.append(to)
                    if to == "cancelled":
                        sets.append("cancel_reason=?"); params.append(reason)
                    self._event(oid, "status", cur, to, actor, reason or str(p.get("note") or ""))
                if "payment" in p:
                    pay = p["payment"] or {}
                    ps, pm = pay.get("status"), pay.get("method")
                    if ps not in PAYMENT_STATUS:
                        raise OrderError("payment status must be unpaid, paid or refunded")
                    if cur == "cancelled" and ps == "paid":
                        raise OrderError("A cancelled order can't be paid", 409)
                    if ps == "refunded" and r["payment_status"] != "paid":
                        raise OrderError("Only a paid order can be refunded", 409)
                    if ps == "paid" and pm not in PAYMENT_METHODS:
                        raise OrderError("choose how it was paid: cash, card, upi or other")
                    sets += ["payment_status=?", "payment_method=?", "paid_at=?"]
                    params += [ps, pm if ps != "unpaid" else None, now_iso() if ps == "paid" else (r["paid_at"] if ps == "refunded" else None)]
                    self._event(oid, "payment", actor=actor, note=f"{ps}{' · ' + pm if pm and ps != 'unpaid' else ''}")
                if sets:
                    sets += ["updated_at=?", "version=version+1"]; params.append(now_iso())
                    self.db.execute(f"UPDATE orders SET {', '.join(sets)} WHERE id=?", params + [oid])
                    self._bump()
                self.db.execute("COMMIT")
            except Exception:
                self.db.execute("ROLLBACK")
                raise
        return self.get(oid)

    def clear_table(self, table: str, actor: str = "staff") -> int:
        """Staff clears a table: every open order there is marked delivered (served)."""
        n = 0
        with self.lock:
            ids = [r[0] for r in self.db.execute(f"SELECT id FROM orders WHERE table_no=? AND status IN ({','.join('?' * len(ACTIVE))})", [str(table), *ACTIVE])]
        for oid in ids:
            self.patch(oid, {"status": "delivered", "note": "table cleared"}, actor); n += 1
        return n

    # ───────────────────────── export ─────────────────────────
    def export_csv(self, f: dict) -> str:
        data = self.list({**f, "limit": 500, "offset": 0, "sort": "oldest"})["orders"]
        out = io.StringIO(); w = csv.writer(out)
        w.writerow(["Order", "Placed (UTC)", "Table", "Source", "Status", "Payment", "Method", "Items", "Subtotal", "Tax", "Total", "Currency", "Notes", "Cancel reason"])
        for o in data:
            w.writerow([o["number"], o["created_at"], o["table_no"], o["source"], o["status"], o["payment_status"], o["payment_method"] or "",
                        "; ".join(f"{i['qty']}x {i['name']}{' (' + i['notes'] + ')' if i['notes'] else ''}" for i in o["items"]),
                        o["subtotal"], o["tax"], o["total"], o["currency"], o["notes"], o["cancel_reason"] or ""])
        return out.getvalue()


def _ts(iso: str) -> float:
    return datetime.strptime(iso, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc).timestamp()
