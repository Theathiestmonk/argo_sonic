"""
Seeds the minimum data sonic_agent.py needs to persist anything to the
database: one client, one location, one robot model, one robot (matching
ROBOT_UID from .env), five demo tables (service_points), and the venue's
live menu (src/argo_mini/menu/menu.json — the same file staff edit via
menu.html/backend/launcher.py) loaded into menu_categories / menu_items.

Idempotent: safe to re-run (e.g. after staff edit the menu) — re-running
refreshes menu_items/menu_categories from the current file; everything else
is looked up before insert so it doesn't create duplicates.

Usage:
    python seed_db.py
"""

import json
import os
import sys
import uuid

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.environ.get("DATABASE_URL")
ROBOT_UID = os.environ.get("ROBOT_UID", "SONIC-001")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(BASE_DIR)
# The launcher/menu.html's live file — NOT a copy bundled with this package —
# so staff edits made through the dashboard are what gets seeded.
MENU_PATH = os.path.join(REPO_ROOT, "src", "argo_mini", "menu", "menu.json")

CLIENT_NAME = "Sonic Demo Restaurant"
LOCATION_NAME = "Main Branch"
MODEL_NAME = "Sonic"
NUM_TABLES = 5

# Fixed namespace so a menu item's frontend-generated "id" (e.g.
# "m1784625561831-1") always maps to the same menu_items.menu_item_id UUID
# across re-seeds, without a separate id-mapping table.
MENU_UUID_NAMESPACE = uuid.UUID("6f6a1e2e-4b8a-5e3a-9c2a-2f2f2f2f2f2f")


def menu_item_uuid(item_id: str) -> str:
    return str(uuid.uuid5(MENU_UUID_NAMESPACE, item_id))


def get_or_create_client(cur):
    cur.execute("SELECT client_id FROM clients WHERE company_name = %s", (CLIENT_NAME,))
    row = cur.fetchone()
    if row:
        return row[0]
    cur.execute(
        "INSERT INTO clients (company_name, business_type, account_status) VALUES (%s, 'restaurant', 'active') RETURNING client_id",
        (CLIENT_NAME,),
    )
    return cur.fetchone()[0]


def get_or_create_location(cur, client_id):
    cur.execute(
        "SELECT location_id FROM locations WHERE client_id = %s AND location_name = %s",
        (client_id, LOCATION_NAME),
    )
    row = cur.fetchone()
    if row:
        return row[0]
    cur.execute(
        "INSERT INTO locations (client_id, location_name, location_type) VALUES (%s, %s, 'restaurant') RETURNING location_id",
        (client_id, LOCATION_NAME),
    )
    return cur.fetchone()[0]


def get_or_create_robot_model(cur):
    cur.execute("SELECT model_id FROM robot_models WHERE model_name = %s", (MODEL_NAME,))
    row = cur.fetchone()
    if row:
        return row[0]
    cur.execute(
        "INSERT INTO robot_models (model_name, model_version, is_active) VALUES (%s, 'v1', true) RETURNING model_id",
        (MODEL_NAME,),
    )
    return cur.fetchone()[0]


def get_or_create_robot(cur, model_id, client_id, location_id):
    cur.execute("SELECT robot_id, location_id FROM robots WHERE robot_uid = %s", (ROBOT_UID,))
    row = cur.fetchone()
    if row:
        return row[0]
    cur.execute(
        """INSERT INTO robots (robot_uid, model_id, client_id, location_id, serial_number, current_status)
           VALUES (%s, %s, %s, %s, %s, 'active') RETURNING robot_id""",
        (ROBOT_UID, model_id, client_id, location_id, f"{ROBOT_UID}-SN"),
    )
    return cur.fetchone()[0]


def get_or_create_service_points(cur, location_id, count):
    ids = []
    for n in range(1, count + 1):
        label = f"Table {n}"
        cur.execute(
            "SELECT service_point_id FROM service_points WHERE location_id = %s AND label = %s",
            (location_id, label),
        )
        row = cur.fetchone()
        if row:
            ids.append(row[0])
            continue
        cur.execute(
            """INSERT INTO service_points (location_id, point_type, label, seating_capacity, is_active)
               VALUES (%s, 'dine_in_table', %s, 4, true) RETURNING service_point_id""",
            (location_id, label),
        )
        ids.append(cur.fetchone()[0])
    return ids


def get_or_create_category(cur, location_id, name, cache):
    if name in cache:
        return cache[name]
    cur.execute(
        "SELECT category_id FROM menu_categories WHERE location_id = %s AND name = %s",
        (location_id, name),
    )
    row = cur.fetchone()
    if row:
        cache[name] = row[0]
        return row[0]
    cur.execute(
        "INSERT INTO menu_categories (location_id, name) VALUES (%s, %s) RETURNING category_id",
        (location_id, name),
    )
    cache[name] = cur.fetchone()[0]
    return cache[name]


def seed_menu_settings(cur, location_id, settings, saved_at):
    cur.execute(
        """INSERT INTO menu_settings (location_id, currency_code, tax_percent, saved_at)
           VALUES (%s, %s, %s, %s)
           ON CONFLICT (location_id) DO UPDATE SET
               currency_code = EXCLUDED.currency_code,
               tax_percent = EXCLUDED.tax_percent,
               saved_at = EXCLUDED.saved_at""",
        (location_id, (settings or {}).get("currencyCode", "USD"),
         (settings or {}).get("taxPercent", 0), saved_at),
    )


def seed_menu(cur, location_id):
    """Reads the launcher/menu.html live menu.json — shape is
    {"menu": [...], "settings": {...}, "savedAt": str} where each item is
    {"id", "name", "price", "category", "desc", "image", "discountPercent",
    "discountActive", "eventLabel", "stock", "available", "diet"} — NOT the
    old aakhri-repo menu.json shape (no "item_id"/"aliases" fields here)."""
    with open(MENU_PATH, encoding="utf-8") as f:
        data = json.load(f)

    seed_menu_settings(cur, location_id, data.get("settings"), data.get("savedAt"))

    category_cache = {}
    n_items = 0

    for item in data.get("menu", []):
        category_name = (item.get("category") or "Uncategorized").strip() or "Uncategorized"
        category_id = get_or_create_category(cur, location_id, category_name, category_cache)
        menu_item_id = menu_item_uuid(item["id"])
        extra = {
            "discountPercent": item.get("discountPercent"),
            "discountActive": item.get("discountActive", False),
            "eventLabel": item.get("eventLabel"),
            "stock": item.get("stock"),
            "diet": item.get("diet"),
        }

        cur.execute(
            """INSERT INTO menu_items (menu_item_id, category_id, location_id, item_name,
                                        description, price, is_available, image_url, extra)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (menu_item_id) DO UPDATE SET
                   category_id = EXCLUDED.category_id,
                   item_name = EXCLUDED.item_name,
                   description = EXCLUDED.description,
                   price = EXCLUDED.price,
                   is_available = EXCLUDED.is_available,
                   image_url = EXCLUDED.image_url,
                   extra = EXCLUDED.extra""",
            (menu_item_id, category_id, location_id, item["name"], item.get("desc"),
             item["price"], item.get("available", True), item.get("image"),
             psycopg2.extras.Json(extra)),
        )
        n_items += 1

    return n_items


def main():
    if not DATABASE_URL:
        print("DATABASE_URL not set. Add it to your .env file first (see .env.example).")
        sys.exit(1)
    if not os.path.exists(MENU_PATH):
        print(f"menu.json not found at {MENU_PATH}")
        sys.exit(1)

    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = False

    try:
        with conn.cursor() as cur:
            client_id = get_or_create_client(cur)
            location_id = get_or_create_location(cur, client_id)
            model_id = get_or_create_robot_model(cur)
            robot_id = get_or_create_robot(cur, model_id, client_id, location_id)
            table_ids = get_or_create_service_points(cur, location_id, NUM_TABLES)
            n_items = seed_menu(cur, location_id)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    print("Seed complete:")
    print(f"  client_id   = {client_id}")
    print(f"  location_id = {location_id}")
    print(f"  robot_id    = {robot_id}  (robot_uid = {ROBOT_UID!r})")
    print(f"  {len(table_ids)} service_points (Table 1..Table {NUM_TABLES})")
    print(f"  {n_items} menu_items loaded from {MENU_PATH}")
    print(f"\nMake sure ROBOT_UID={ROBOT_UID!r} is set in your .env (it already defaults to this).")


if __name__ == "__main__":
    main()
