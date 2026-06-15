"""
POS (Point of Sale) integration.

Priority:
1. REST API (configured endpoint)
2. WebSocket API (fallback)
3. Local SQLite database (offline fallback)
"""

import json
import logging
import os
import sqlite3
import time
import uuid
from typing import List, Optional, Dict, Any

import requests

logger = logging.getLogger("argo_pos")

_DB_PATH = os.path.expanduser("~/argo_mini_ws/restaurant_orders.db")


# ── Local SQLite fallback ──────────────────────────────────────────────────────

def _init_db(path: str = _DB_PATH):
    conn = sqlite3.connect(path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            order_id    TEXT PRIMARY KEY,
            session_id  TEXT,
            table_id    TEXT,
            customer    TEXT,
            phone       TEXT,
            items       TEXT,
            total       REAL,
            status      TEXT,
            created_at  REAL,
            updated_at  REAL
        )
    """)
    conn.commit()
    return conn


class POSClient:
    """
    POS integration with automatic fallback chain:
    REST API → Local SQLite
    """

    def __init__(
        self,
        api_url: str = "",
        api_key: str = "",
        db_path: str = _DB_PATH,
    ):
        self.api_url = api_url or os.environ.get("POS_API_URL", "")
        self.api_key = api_key or os.environ.get("POS_API_KEY", "")
        self.db_path = db_path
        self._session = requests.Session()
        if self.api_key:
            self._session.headers["Authorization"] = f"Bearer {self.api_key}"

    # ── Public API ─────────────────────────────────────────────────────────────

    def create_order(
        self,
        table_id: str,
        customer_name: str,
        phone: str,
        items: List[Dict],
        session_id: str = "",
    ) -> Optional[str]:
        """Submit order and return order_id. Returns None on failure."""
        order_id = f"ORD-{str(uuid.uuid4())[:6].upper()}"
        total = sum(i["unit_price"] * i["quantity"] for i in items)
        payload = {
            "order_id": order_id,
            "session_id": session_id,
            "table_id": table_id,
            "customer_name": customer_name,
            "phone": phone,
            "items": items,
            "total": total,
            "status": "confirmed",
            "created_at": time.time(),
        }

        # Try REST API
        if self.api_url:
            try:
                resp = self._session.post(
                    f"{self.api_url}/orders",
                    json=payload,
                    timeout=5,
                )
                resp.raise_for_status()
                data = resp.json()
                logger.info(f"[POS] Order sent via API: {data.get('order_id', order_id)}")
                return data.get("order_id", order_id)
            except Exception as e:
                logger.warning(f"[POS] API failed ({e}), falling back to local DB")

        # Local SQLite fallback
        return self._save_local(payload)

    def get_order(self, order_id: str) -> Optional[Dict]:
        """Fetch order by ID."""
        if self.api_url:
            try:
                resp = self._session.get(f"{self.api_url}/orders/{order_id}", timeout=5)
                resp.raise_for_status()
                return resp.json()
            except Exception:
                pass
        return self._get_local(order_id)

    def update_order_status(self, order_id: str, status: str) -> bool:
        """Update order status (confirmed / preparing / ready / delivered)."""
        if self.api_url:
            try:
                resp = self._session.patch(
                    f"{self.api_url}/orders/{order_id}",
                    json={"status": status},
                    timeout=5,
                )
                resp.raise_for_status()
                return True
            except Exception:
                pass
        return self._update_local_status(order_id, status)

    def generate_bill(self, order_id: str) -> Optional[Dict]:
        """Generate final bill for order."""
        order = self.get_order(order_id)
        if not order:
            return None
        items = order.get("items", [])
        subtotal = sum(i["unit_price"] * i["quantity"] for i in items)
        tax = round(subtotal * 0.05, 2)   # 5% GST
        total = round(subtotal + tax, 2)
        return {
            "order_id": order_id,
            "table_id": order.get("table_id"),
            "customer": order.get("customer_name"),
            "items": items,
            "subtotal": subtotal,
            "tax": tax,
            "total": total,
        }

    def get_all_orders(self) -> List[Dict]:
        """List all orders (for kitchen dashboard)."""
        conn = _init_db(self.db_path)
        rows = conn.execute(
            "SELECT * FROM orders ORDER BY created_at DESC LIMIT 50"
        ).fetchall()
        conn.close()
        cols = ["order_id","session_id","table_id","customer","phone",
                "items","total","status","created_at","updated_at"]
        result = []
        for row in rows:
            d = dict(zip(cols, row))
            d["items"] = json.loads(d["items"])
            result.append(d)
        return result

    # ── Local DB helpers ──────────────────────────────────────────────────────

    def _save_local(self, payload: Dict) -> Optional[str]:
        try:
            conn = _init_db(self.db_path)
            conn.execute("""
                INSERT INTO orders VALUES (?,?,?,?,?,?,?,?,?,?)
            """, (
                payload["order_id"], payload["session_id"],
                payload["table_id"], payload["customer_name"],
                payload["phone"], json.dumps(payload["items"]),
                payload["total"], payload["status"],
                payload["created_at"], payload["created_at"],
            ))
            conn.commit()
            conn.close()
            logger.info(f"[POS] Order saved locally: {payload['order_id']}")
            return payload["order_id"]
        except Exception as e:
            logger.error(f"[POS] Local DB save failed: {e}")
            return None

    def _get_local(self, order_id: str) -> Optional[Dict]:
        try:
            conn = _init_db(self.db_path)
            row = conn.execute(
                "SELECT * FROM orders WHERE order_id=?", (order_id,)
            ).fetchone()
            conn.close()
            if not row:
                return None
            cols = ["order_id","session_id","table_id","customer_name","phone",
                    "items","total","status","created_at","updated_at"]
            d = dict(zip(cols, row))
            d["items"] = json.loads(d["items"])
            return d
        except Exception:
            return None

    def _update_local_status(self, order_id: str, status: str) -> bool:
        try:
            conn = _init_db(self.db_path)
            conn.execute(
                "UPDATE orders SET status=?, updated_at=? WHERE order_id=?",
                (status, time.time(), order_id),
            )
            conn.commit()
            conn.close()
            return True
        except Exception:
            return False
