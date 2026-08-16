"""Intent-specific execute handlers — run once a Payload is
is_complete_for_intent(). Each returns the text that should be spoken;
callers are responsible for actually speaking it (main_agent_v2.py) so this
module stays testable without any audio dependency.

Only TAKE_ORDER persists to Postgres (orders/order_items, same tables/
pattern main_agent.py's db_place_order() uses) — matches this project's
existing scope decision that take_order is the one intent built out fully;
the rest speak a reasonable response without needing new schema.
"""

from __future__ import annotations

import uuid as uuid_module
from datetime import datetime
from typing import Optional

import db as db_module
import menu_loader
from db import db
from llm_client import LLMClient
from models import Payload
from payload_manager import calculate_total
from prompt_generator import render_announcement


def _table_service_point_id(location_id: str, table_no: int) -> Optional[str]:
    conn = db()
    if conn is None:
        return None
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT service_point_id FROM service_points WHERE location_id = %s AND label = %s",
                (location_id, f"Table {table_no}"),
            )
            row = cur.fetchone()
            return str(row[0]) if row else None
    except Exception as e:
        print(f"[warn] service_point lookup failed: {e}")
        return None


def _active_visit_id(service_point_id: str) -> Optional[str]:
    conn = db()
    if conn is None:
        return None
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT visit_id FROM visits WHERE service_point_id = %s AND visit_status = 'active'
                   ORDER BY checked_in_at DESC LIMIT 1""",
                (service_point_id,),
            )
            row = cur.fetchone()
            if row:
                return str(row[0])
            cur.execute(
                """INSERT INTO visits (location_id, service_point_id, visit_status, checked_in_at)
                   VALUES (%s, %s, 'active', now()) RETURNING visit_id""",
                (db_module.DB_LOCATION_ID, service_point_id),
            )
            return str(cur.fetchone()[0])
    except Exception as e:
        print(f"[warn] visit resolve failed: {e}")
        return None


def _place_order(payload: Payload, total: float) -> Optional[str]:
    location_id = db_module.DB_LOCATION_ID
    conn = db()
    if conn is None or location_id is None or payload.order_table is None:
        return None
    service_point_id = _table_service_point_id(location_id, payload.order_table)
    visit_id = _active_visit_id(service_point_id) if service_point_id else None
    order_number = f"ORD-{datetime.now():%Y%m%d}-{uuid_module.uuid4().hex[:6].upper()}"
    try:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO orders (order_number, location_id, visit_id, service_point_id,
                                        order_source, order_status, subtotal_amount, total_amount, confirmed_at)
                   VALUES (%s, %s, %s, %s, 'voice_assistant', 'confirmed', %s, %s, now())
                   RETURNING order_id""",
                (order_number, location_id, visit_id, service_point_id, total, total),
            )
            order_id = cur.fetchone()[0]
            for item in payload.order_items.values():
                menu_item = menu_loader.find_item(item.name)
                if menu_item is None:
                    print(f"[warn] order item {item.name!r} didn't match any menu item — "
                          f"dropped from order {order_number}, not billed or sent to kitchen")
                    continue
                unit_price = menu_item["price"]
                special_request = (
                    ", ".join(f"{k}: {v}" for k, v in item.modifications.items())
                    if item.modifications else None
                )
                cur.execute(
                    """INSERT INTO order_items (order_id, menu_item_id, quantity, unit_price, line_total,
                                                 special_request)
                       VALUES (%s, %s, %s, %s, %s, %s)""",
                    (order_id, menu_item["item_id"], item.qty, unit_price,
                     unit_price * item.qty, special_request),
                )
        return order_number
    except Exception as e:
        print(f"[warn] DB order write failed: {e}")
        return None


def execute_take_order(llm: LLMClient, payload: Payload) -> str:
    total = calculate_total(payload, menu_loader.get_price)
    payload.order_total = total
    _place_order(payload, total)
    currency = menu_loader.CURRENCY_SYMBOL
    return render_announcement(
        llm, "order_confirmed", payload,
        order_summary=payload.order_summary(), total=f"{currency}{total:.2f}",
    )


def execute_tell_menu(llm: LLMClient, payload: Payload) -> str:
    return render_announcement(llm, "farewell", payload)


def execute_navigate(llm: LLMClient, payload: Payload) -> str:
    return render_announcement(llm, "arrived_at_table", payload, table=payload.order_table)


def execute_get_bill(llm: LLMClient, payload: Payload) -> str:
    location_id = db_module.DB_LOCATION_ID
    conn = db()
    total = 0.0
    if conn is not None and location_id is not None and payload.order_table is not None:
        service_point_id = _table_service_point_id(location_id, payload.order_table)
        if service_point_id:
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        """SELECT COALESCE(SUM(o.total_amount), 0) FROM orders o
                           WHERE o.service_point_id = %s AND o.order_status = 'confirmed'""",
                        (service_point_id,),
                    )
                    total = float(cur.fetchone()[0])
            except Exception as e:
                print(f"[warn] bill lookup failed: {e}")
    currency = menu_loader.CURRENCY_SYMBOL
    return render_announcement(llm, "get_bill", payload, order_summary=payload.order_summary(),
                                total=f"{currency}{total:.2f}")


def execute_deliver_order(llm: LLMClient, payload: Payload) -> str:
    return render_announcement(llm, "order_confirmed", payload,
                                order_summary=payload.order_summary(), total="")


def execute_about_cafe(llm: LLMClient, payload: Payload) -> str:
    about_text = (
        "RoboBrew is Ahmedabad's first robot cafe — coffee, cookies, and bakery items, "
        "all made and served by robots."
    )
    return render_announcement(llm, "about_cafe", payload, about_text=about_text)


EXECUTORS = {
    "take_order": execute_take_order,
    "tell_menu": execute_tell_menu,
    "navigate": execute_navigate,
    "get_bill": execute_get_bill,
    "deliver_order": execute_deliver_order,
    "about_cafe": execute_about_cafe,
}


def execute(llm: LLMClient, payload: Payload) -> str:
    handler = EXECUTORS.get(payload.intent.value)
    if handler is None:
        return render_announcement(llm, "farewell", payload)
    return handler(llm, payload)
