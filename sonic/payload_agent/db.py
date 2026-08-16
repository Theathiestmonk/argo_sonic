"""Postgres connection + robot/location resolution — same pattern and
schema main_agent.py uses (robots/locations/menu_items/orders tables),
kept separate here so menu_loader/executor don't need to import the whole
main_agent module just for a connection handle."""

from __future__ import annotations

import os
from typing import Optional

import psycopg2

DATABASE_URL = os.environ.get("DATABASE_URL")
ROBOT_UID = os.environ.get("ROBOT_UID", "SONIC-001")

DB_ROBOT_ID: Optional[str] = None
DB_LOCATION_ID: Optional[str] = None
_db_conn = None


def db():
    global _db_conn
    if not DATABASE_URL:
        return None
    if _db_conn is None or _db_conn.closed:
        try:
            _db_conn = psycopg2.connect(DATABASE_URL)
            _db_conn.autocommit = True
        except Exception as e:
            print(f"[warn] DB connection failed: {e}")
            _db_conn = None
    return _db_conn


def resolve_robot() -> None:
    global DB_ROBOT_ID, DB_LOCATION_ID
    conn = db()
    if conn is None:
        return
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT robot_id, location_id FROM robots WHERE robot_uid = %s", (ROBOT_UID,))
            row = cur.fetchone()
        if row is None:
            print(f"[warn] No robot row for ROBOT_UID={ROBOT_UID!r} — DB persistence disabled for this run.")
            return
        DB_ROBOT_ID, DB_LOCATION_ID = str(row[0]), str(row[1])
        print(f"[db] Connected — robot_id={DB_ROBOT_ID}, location_id={DB_LOCATION_ID}")
    except Exception as e:
        print(f"[warn] DB robot lookup failed: {e}")
