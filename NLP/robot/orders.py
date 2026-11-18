"""Where a confirmed order goes. Postgres is what the dashboard reads today (see backend/launcher.py
_read_orders_db); the legacy per-map JSON file is the offline fallback."""
from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from datetime import datetime, timezone
from typing import Protocol

from ..config import Settings

log = logging.getLogger("nlp.orders")


class OrderSink(Protocol):
    async def submit(self, table: int, lines: list[dict]) -> str: ...


def _total(lines: list[dict]) -> float:
    return round(sum(float(l.get("price") or 0) * int(l["qty"]) for l in lines), 2)


class FileOrderSink:
    """{table: {items, total, status, updatedAt}} in orders/<map>.json – same shape the old UI used."""

    def __init__(self, s: Settings, map_getter=None):
        self.s, self._map = s, map_getter or (lambda: s.map_name)

    async def submit(self, table: int, lines: list[dict]) -> str:
        def run() -> str:
            self.path = self.s.orders_dir / f"{self._map()}.json"
            self.path.parent.mkdir(parents=True, exist_ok=True)
            try:
                data = json.loads(self.path.read_text())
            except Exception:
                data = {}
            cur = data.get(str(table), {"items": [], "total": 0})
            items = cur.get("items", [])
            for l in lines:
                items.append({"id": l.get("menu_id") or l["name"], "name": l["name"] + (f" ({l['variation']})" if l.get("variation") else ""),
                              "qty": l["qty"], "price": l.get("price") or 0})
            data[str(table)] = {"items": items, "total": round(sum(i["price"] * i["qty"] for i in items), 2),
                                "status": "submitted", "updatedAt": datetime.now(timezone.utc).isoformat()}
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=2))
            tmp.replace(self.path)
            return f"file:{table}"
        return await asyncio.to_thread(run)


class PostgresOrderSink:
    """Inserts orders/order_items the way the schema expects (visit per table, source 'voice_assistant')."""

    def __init__(self, s: Settings, dry_run: bool = False):
        self.s, self.dry_run = s, dry_run

    def _submit(self, table: int, lines: list[dict]) -> str:
        import psycopg2
        conn = psycopg2.connect(self.s.database_url, connect_timeout=2)
        try:
            with conn, conn.cursor() as cur:
                cur.execute("SELECT location_id FROM robots WHERE robot_uid = %s", (self.s.robot_uid,))
                row = cur.fetchone()
                if not row:
                    raise RuntimeError(f"no robot {self.s.robot_uid!r}")
                loc = row[0]
                cur.execute("SELECT service_point_id FROM service_points WHERE location_id=%s AND label=%s", (loc, f"Table {table}"))
                row = cur.fetchone()
                if not row:
                    raise RuntimeError(f"no service point 'Table {table}'")
                sp = row[0]
                cur.execute("SELECT visit_id FROM visits WHERE service_point_id=%s AND visit_status='active' LIMIT 1", (sp,))
                row = cur.fetchone()
                if row:
                    visit = row[0]
                else:
                    cur.execute("INSERT INTO visits (location_id, service_point_id) VALUES (%s,%s) RETURNING visit_id", (loc, sp))
                    visit = cur.fetchone()[0]
                menu_lines = [l for l in lines if l.get("kind") == "menu"]
                extras = [f"{l['qty']} {l['name']}" for l in lines if l.get("kind") != "menu"]
                total = _total(menu_lines)
                number = f"ORD-{time.strftime('%Y%m%d')}-{random.randint(0, 99999):05d}"
                cur.execute(
                    """INSERT INTO orders (order_number, location_id, visit_id, service_point_id, order_source, order_status,
                                           subtotal_amount, tax_amount, total_amount, special_instructions)
                       VALUES (%s,%s,%s,%s,'voice_assistant','placed',%s,0,%s,%s) RETURNING order_id""",
                    (number, loc, visit, sp, total, total, ("Also bring: " + ", ".join(extras)) if extras else None))
                oid = cur.fetchone()[0]
                for l in menu_lines:
                    cur.execute("SELECT menu_item_id FROM menu_items WHERE location_id=%s AND lower(item_name)=lower(%s) LIMIT 1", (loc, l["name"]))
                    m = cur.fetchone()
                    if not m:
                        log.warning("menu item %r not in DB – added as special instruction", l["name"])
                        cur.execute("UPDATE orders SET special_instructions = coalesce(special_instructions||'; ','') || %s WHERE order_id=%s",
                                    (f"{l['qty']} {l['name']}", oid))
                        continue
                    price = float(l.get("price") or 0)
                    cur.execute(
                        """INSERT INTO order_items (order_id, menu_item_id, quantity, unit_price, line_total, special_request)
                           VALUES (%s,%s,%s,%s,%s,%s)""",
                        (oid, m[0], l["qty"], price, round(price * l["qty"], 2), l.get("variation")))
                cur.execute("INSERT INTO order_status_history (order_id, status, note) VALUES (%s,'placed','voice assistant')", (oid,))
                if self.dry_run:
                    raise _DryRun(number)
                return number
        except _DryRun as d:
            return f"dry-run:{d.args[0]}"
        finally:
            conn.close()

    async def submit(self, table: int, lines: list[dict]) -> str:
        return await asyncio.to_thread(self._submit, table, lines)


class _DryRun(Exception):
    pass


class LedgerOrderSink:
    """The dashboard's POS order ledger (backend/pos_api.py). Orders arrive as 'placed' with source voice_assistant,
    get a number (#1001 …) and show up on the Orders page and the dashboard immediately."""

    def __init__(self, s: Settings, map_getter=None):
        self.url, self._map = s.pos_api_url.rstrip("/"), map_getter or (lambda: s.map_name)

    async def submit(self, table: int, lines: list[dict]) -> str:
        import httpx
        items = [{"name": l["name"], "qty": l["qty"], "price": l.get("price"), "menu_id": l.get("menu_id") or "", "notes": l.get("variation") or "",
                  "kind": l.get("kind") or "menu"} for l in lines]
        async with httpx.AsyncClient(timeout=4.0) as c:
            r = await c.post(f"{self.url}/orders", json={"table": str(table), "items": items, "source": "voice_assistant",
                                                         "actor": "voice", "map": self._map()})
        if r.status_code != 201:
            raise RuntimeError(f"ledger rejected the order: {r.status_code} {r.text[:120]}")
        return f"ledger:#{r.json()['number']}"


class FallbackSink:
    """Try each sink in order; an order is never lost – the legacy file is the last resort."""

    def __init__(self, *sinks: OrderSink):
        self.sinks = sinks

    async def submit(self, table: int, lines: list[dict]) -> str:
        last = None
        for i, sink in enumerate(self.sinks):
            try:
                return await sink.submit(table, lines)
            except Exception as e:
                last = e
                log.warning("order sink %s failed (%s)%s", type(sink).__name__, e, " – trying the next one" if i < len(self.sinks) - 1 else "")
        raise last


def make_sink(s: Settings, map_getter=None) -> OrderSink:
    f = FileOrderSink(s, map_getter)
    if s.order_sink == "file":
        return f
    chain: list = []
    if s.order_sink in ("auto", "ledger"):
        chain.append(LedgerOrderSink(s, map_getter))
    if s.database_url and s.order_sink in ("auto", "postgres"):
        chain.append(PostgresOrderSink(s))
    return FallbackSink(*chain, f) if chain else f
