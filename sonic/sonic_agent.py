"""
Sonic — real-time voice NLP agent for a restaurant service robot.

Flow: wake word ("Hi Sonic", via Hi_Sonic.onnx) -> greet -> listen -> Groq LLM
NLU (intent classification + slot extraction, with mid-conversation intent
switching) -> dialogue handler (menu / call_people / take_order / navigation /
cutlery / about / normal_conv / get_bill) -> Sarvam TTS -> back to idle.

Runtime: a real LangGraph StateGraph (see build_graph()) — each step of the
flowchart (ask_item, ask_qty, confirm_order, etc.) is an actual graph node.
Multi-turn "ask the guest something and wait" is implemented with LangGraph's
interrupt()/Command(resume=...) human-in-the-loop mechanism plus a
MemorySaver checkpointer: a node calls interrupt() to pause, the outer driver
(run_graph_session) speaks the prompt and listens for a reply, then resumes
the graph with the answer (or a timeout sentinel). Node-by-node execution is
traced to the terminal by default (see trace_node()).

An order (current_order) is never cleared just because it was confirmed —
confirmed items are marked, not removed, so a later "add naan" still shows
up alongside earlier items. Only an explicit whole-order cancellation
(edit_order -> cancel_full) clears it.

Every prompt the robot asks waits up to SILENCE_ONSET_TIMEOUT_S seconds for a
reply; on silence it repeats the question once, then falls back to idle.

Run modes:
    python sonic_agent.py               real mic/speaker/wake-word loop
    python sonic_agent.py --text-mode   typed input / printed output (no mic,
                                         no wake word, no Sarvam calls) — for
                                         exercising the dialogue logic directly

If TABLE_NO is set (backend/launcher.py sets this when staff dispatch the
robot to a specific table from the dashboard), the wake-word gate is skipped
and the robot runs one real Kitchen -> Table N -> Kitchen round trip (via
navigate_and_wait(), which blocks on an actual Nav2 result — see
nav_bridge.py) before exiting instead of looping on the wake word — see
main_loop(). SONIC_ACTION_HINT selects order/bill/room_service (round trip
+ normal conversation) vs. deliver (run_delivery_session(): wait at the
kitchen for a spoken pickup confirmation before departing).

Menu lives in Postgres (menu_items/menu_categories, see load_menu_from_db())
instead of a bundled file — requires DATABASE_URL and `seed_db.py` to have
been run first. Requires GROQ_API_KEY always, and SARVAM_API_KEY for real
voice mode (see .env.example).
"""

from __future__ import annotations

import argparse
import base64
import difflib
import io
import json
import os
import queue
import random
import re
import shlex
import subprocess
import sys
import threading
import time
import uuid as uuid_module
import wave
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Optional

import numpy as np
import psycopg2
import psycopg2.extras
import requests
import sounddevice as sd
from dotenv import load_dotenv
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, StateGraph
from langgraph.types import Command, interrupt

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

if sys.platform == "win32":
    # Windows consoles default to a codepage that can't encode arbitrary
    # unicode (e.g. currency symbols, emoji) that an LLM response might contain.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

load_dotenv()

GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
SARVAM_API_KEY = os.environ.get("SARVAM_API_KEY")

GROQ_MODEL = "llama-3.3-70b-versatile"
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

SARVAM_STT_URL = "https://api.sarvam.ai/speech-to-text"
SARVAM_TTS_URL = "https://api.sarvam.ai/text-to-speech"
SARVAM_STT_MODEL = "saarika:v2.5"   # "saarika:v2" is deprecated by Sarvam
SARVAM_TTS_MODEL = "bulbul:v3"
SARVAM_LANGUAGE_CODE = "en-IN"
# ishita = bulbul:v3's female speaker. shubh (the previous default here) is
# male; varun was considered even earlier but Sarvam's own docs flag it as
# carrying a "deep, dramatic villain/suspense character" — not a fit for a
# friendly assistant robot.
SARVAM_SPEAKER = "ishita"
SARVAM_TTS_PACE = 1.0  # 1.0 = normal speed
SARVAM_TTS_WS_URL = "wss://api.sarvam.ai/text-to-speech/ws"
SARVAM_TTS_SAMPLE_RATE = 22050  # one of Sarvam's supported rates: 8000/16000/22050/24000

WAKE_WORD_MODEL_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "Hi_Sonic.onnx")
WAKE_WORD_NAME = "Hi_Sonic"
WAKE_WORD_THRESHOLD = 0.5

# navigate_and_wait() shells out to this — the one piece of sonic_agent.py
# that needs the ROS environment, since this file itself deliberately stays
# a plain non-ROS process (see nav_bridge.py's own docstring).
NAV_BRIDGE_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "nav_bridge.py")
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NAV_TIMEOUT_S = 90.0

# Menu now lives in Postgres (menu_categories/menu_items) instead of a bundled
# file — MENU_CATEGORIES is populated dynamically by load_menu_from_db() below
# from whatever categories the venue's staff have actually created, since a
# fixed cuisine-specific set (starters/soups/...) doesn't generalize across
# venues (e.g. a burger joint's categories look nothing like a cafe's).
MENU_CATEGORIES: set = set()
VALID_CUTLERY = {"spoon", "fork", "glass", "knife", "napkin"}
VALID_CALL_TARGETS = {"manager", "waiter", "cleaner"}
# Categories that get brought without the usual quantity/preference
# interrogation — nobody asks "what spice level?" for a bottle of water.
QUICK_SERVE_CATEGORIES = {"drinks", "beverages"}

SAMPLE_RATE = 16000
FRAME_MS = 30
FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS // 1000  # 480 samples/frame

SILENCE_ONSET_TIMEOUT_S = 5.0
TRAILING_SILENCE_MS = 600
SPEECH_RMS_THRESHOLD = 500  # int16 RMS energy; recalibrate against your mic/room

TEXT_MODE = False

# Node/function-flow tracing — prints every LangGraph node as it runs, with
# the live order state, so you can watch execution happen in the terminal.
# On by default; set SONIC_TRACE=0 to silence it.
TRACE_ENABLED = os.environ.get("SONIC_TRACE", "1") != "0"


def trace(event: str, **fields) -> None:
    if not TRACE_ENABLED:
        return
    details = " ".join(f"{k}={v!r}" for k, v in fields.items())
    print(f"[trace] {event}" + (f" | {details}" if details else ""))


def order_snapshot(state: "DialogueState") -> str:
    items = ", ".join(
        f"{i['quantity']}x{i['name']}" + ("" if i.get("confirmed") else "*")
        for i in state.current_order
    ) or "empty"
    active = f"{state.active_item['name']}(building)" if state.active_item else "none"
    pending = state.pending_items or []
    return f"current_order=[{items}] (*=not yet confirmed) active_item={active} pending_items={pending}"


# --- Postgres persistence (menu is DB-only now; orders/sessions/etc. still
# degrade gracefully to pure in-memory behavior if DATABASE_URL isn't set) --
DATABASE_URL = os.environ.get("DATABASE_URL")
ROBOT_UID = os.environ.get("ROBOT_UID", "SONIC-001")
# Staff-dispatched sessions (backend/launcher.py spawning one process scoped
# to a specific table) set this so the session skips conversational table
# discovery — see load_menu_from_db()/run_graph_session() below.
FORCED_TABLE_NO = os.environ.get("TABLE_NO") or None
# Which table-action button was clicked ("order"/"deliver"/"bill"/
# "room_service") — set by launcher.py alongside TABLE_NO. Selects between
# the normal kitchen->table->(conversation)->kitchen round trip and the
# dedicated delivery routine (run_delivery_session) in main_loop() below.
FORCED_ACTION = os.environ.get("SONIC_ACTION_HINT") or None
# Which src/argo_mini/waypoints/<MAP_NAME>.json to navigate against —
# nav_bridge.py reads this file for real robot coordinates.
MAP_NAME = os.environ.get("SONIC_MAP_NAME", "office_map")


def require_api_keys(need_sarvam: bool = True) -> None:
    missing = []
    if not GROQ_API_KEY:
        missing.append("GROQ_API_KEY (get one from https://console.groq.com)")
    if need_sarvam and not SARVAM_API_KEY:
        missing.append("SARVAM_API_KEY (get one from https://www.sarvam.ai)")
    if missing:
        print("Missing required API key(s):")
        for m in missing:
            print(f"  - {m}")
        print("Add them to a .env file (copy .env.example) before running.")
        sys.exit(1)


# ---------------------------------------------------------------------------
# Dialogue state
# ---------------------------------------------------------------------------

@dataclass
class DialogueState:
    conversation_history: list = field(default_factory=list)   # [{"role","text"}]
    active_intent: Optional[str] = None
    pending_question: Optional[str] = None
    order_action: Optional[str] = None
    order_slot: Optional[str] = None
    current_order: list = field(default_factory=list)          # cumulative line items for the whole visit
    active_item: Optional[dict] = None                          # item being built/edited
    pending_items: list = field(default_factory=list)          # extra items named in the same breath, queued up
    paused_order: Optional[dict] = None                         # saved sub-state on intent switch
    table_no: Optional[str] = None
    table_no_locked: bool = False   # True once table_no has been captured once — protects against a
                                     # later misheard number silently redirecting the whole visit
    bill_total: float = 0.0

    # DB-backed persistence handles (all None if DATABASE_URL isn't set)
    db_session_id: Optional[str] = None
    db_visit_id: Optional[str] = None
    db_service_point_id: Optional[str] = None

    # --- LangGraph control-flow bookkeeping. Each slot/step is a separate
    # graph node (not a loop iteration), so anything that used to be a plain
    # Python local variable inside a while-loop has to live on state instead,
    # to survive across separate node invocations (including replay-on-resume
    # for interrupted nodes). ---
    item_attempts: int = 0
    pending_seed: Optional[dict] = None       # a not-yet-consumed NLU-shaped outcome for the next node
    last_outcome: Optional[dict] = None       # most recent NLU result, read by the node that asked for it
    next_node: Optional[str] = None           # routing hint a node sets; read by its conditional edge
    went_idle: bool = False                   # set True when this run should end (idle/timeout/give-up)
    first_turn: bool = True                   # gates farewell-detection / greeting wording
    edit_target_name: Optional[str] = None    # which order line edit_order_start resolved, for later sub-nodes
    pending_resume_after_switch: bool = False # set by handle_switch when it paused a take_order flow


# ---------------------------------------------------------------------------
# Database (Postgres) persistence — optional. Every function here is a no-op
# if DATABASE_URL isn't set or the robot/DB isn't reachable, so the rest of
# the system behaves exactly as before (pure in-memory) without a database.
# Schema: robot_fleet_schema.sql + restaurant_ops_schema.sql + db_schema.sql.
# ---------------------------------------------------------------------------

DB_ROBOT_ID: Optional[str] = None
DB_LOCATION_ID: Optional[str] = None

_db_conn = None


def db():
    """Lazily connect (and reconnect if the connection died). Returns None
    if DATABASE_URL isn't set or the connection attempt fails."""
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
    """Looks up the robots row matching ROBOT_UID and caches its id + location.
    Call once at startup. If this doesn't find a match, all DB persistence
    below silently no-ops for the rest of the run."""
    global DB_ROBOT_ID, DB_LOCATION_ID
    conn = db()
    if conn is None:
        return
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT robot_id, location_id FROM robots WHERE robot_uid = %s", (ROBOT_UID,))
            row = cur.fetchone()
        if row is None:
            print(f"[warn] No robot row for ROBOT_UID={ROBOT_UID!r} — DB persistence disabled "
                  f"for this run. Run `python seed_db.py` first.")
            return
        DB_ROBOT_ID, DB_LOCATION_ID = str(row[0]), str(row[1])
        print(f"[db] Connected — robot_id={DB_ROBOT_ID}, location_id={DB_LOCATION_ID}")
    except Exception as e:
        print(f"[warn] DB robot lookup failed: {e}")


def resolve_visit(state: DialogueState) -> None:
    """Once state.table_no is known, resolve (or open) a visit for that
    table's service_point and cache it on state. Backfills the current
    dialogue session's visit/service_point too, since the session row may
    have been created before the table number was known."""
    conn = db()
    if conn is None or DB_LOCATION_ID is None or state.db_visit_id or not state.table_no:
        return
    label = f"Table {state.table_no}"
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT service_point_id FROM service_points WHERE location_id = %s AND label = %s",
                (DB_LOCATION_ID, label),
            )
            row = cur.fetchone()
            if row is None:
                return   # unknown table number — nothing to attach this visit to
            service_point_id = row[0]

            cur.execute(
                """SELECT visit_id FROM visits WHERE service_point_id = %s AND visit_status = 'active'
                   ORDER BY checked_in_at DESC LIMIT 1""",
                (service_point_id,),
            )
            row = cur.fetchone()
            if row:
                visit_id = row[0]
            else:
                cur.execute(
                    "INSERT INTO visits (location_id, service_point_id) VALUES (%s, %s) RETURNING visit_id",
                    (DB_LOCATION_ID, service_point_id),
                )
                visit_id = cur.fetchone()[0]

            state.db_visit_id, state.db_service_point_id = str(visit_id), str(service_point_id)

            if state.db_session_id:
                cur.execute(
                    "UPDATE sonic_dialogue_sessions SET visit_id = %s, service_point_id = %s WHERE session_id = %s",
                    (state.db_visit_id, state.db_service_point_id, state.db_session_id),
                )
    except Exception as e:
        print(f"[warn] DB visit resolution failed: {e}")


def db_start_session(state: DialogueState) -> None:
    conn = db()
    if conn is None or DB_ROBOT_ID is None:
        return
    resolve_visit(state)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO sonic_dialogue_sessions (robot_id, visit_id, service_point_id) VALUES (%s, %s, %s) RETURNING session_id",
                (DB_ROBOT_ID, state.db_visit_id, state.db_service_point_id),
            )
            state.db_session_id = str(cur.fetchone()[0])
    except Exception as e:
        print(f"[warn] DB session start failed: {e}")


def db_end_session(state: DialogueState) -> None:
    conn = db()
    if conn is None or not state.db_session_id:
        return
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE sonic_dialogue_sessions SET ended_at = now() WHERE session_id = %s",
                        (state.db_session_id,))
    except Exception as e:
        print(f"[warn] DB session end failed: {e}")


def db_log_turn(state: DialogueState, role: str, text: str, *,
                 intent: Optional[str] = None, slots: Optional[dict] = None) -> None:
    conn = db()
    if conn is None or not state.db_session_id:
        return
    try:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO sonic_dialogue_turns (session_id, role, text, detected_intent, slots)
                   VALUES (%s, %s, %s, %s, %s)""",
                (state.db_session_id, role, text, intent, psycopg2.extras.Json(slots) if slots else None),
            )
    except Exception as e:
        print(f"[warn] DB turn log failed: {e}")


def db_place_order(state: DialogueState, items: list, total: float) -> None:
    """Writes only the newly-confirmed items (not the whole cumulative
    current_order) as one orders/order_items batch, since orders now persist
    across multiple confirms instead of being cleared each time — a repeat
    confirm must not re-bill/re-insert items already sent to the kitchen."""
    conn = db()
    if conn is None or DB_LOCATION_ID is None:
        return
    resolve_visit(state)
    order_number = f"ORD-{datetime.now():%Y%m%d}-{uuid_module.uuid4().hex[:6].upper()}"
    try:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO orders (order_number, location_id, visit_id, service_point_id,
                                        order_source, order_status, subtotal_amount, total_amount, confirmed_at)
                   VALUES (%s, %s, %s, %s, 'voice_assistant', 'confirmed', %s, %s, now())
                   RETURNING order_id""",
                (order_number, DB_LOCATION_ID, state.db_visit_id, state.db_service_point_id, total, total),
            )
            order_id = cur.fetchone()[0]
            for item in items:
                unit_price = get_item_price(item)
                cur.execute(
                    """INSERT INTO order_items (order_id, menu_item_id, quantity, unit_price, line_total, special_request)
                       VALUES (%s, %s, %s, %s, %s, %s)""",
                    (order_id, item["item_id"], item["quantity"], unit_price,
                     unit_price * item["quantity"], item.get("preference")),
                )
    except Exception as e:
        print(f"[warn] DB order write failed: {e}")


def db_get_bill_total(state: DialogueState) -> Optional[float]:
    conn = db()
    if conn is None or not state.db_visit_id:
        return None
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT bill_total FROM visit_bill_totals WHERE visit_id = %s", (state.db_visit_id,))
            row = cur.fetchone()
            return float(row[0]) if row else None
    except Exception as e:
        print(f"[warn] DB bill lookup failed: {e}")
        return None


def voice_nav_enabled() -> bool:
    """Staff-facing kill-switch (locations.voice_nav_enabled, toggled from
    the dashboard via backend/launcher.py's GET/POST /voice/nav_enabled).
    Gates ALL autonomous navigation now — guest-voice requests ("take me to
    the kitchen") AND staff-dispatched round trips (navigate_and_wait
    below) — a single switch to stop all robot movement (e.g. staff sees a
    spill on the floor). Defaults to True (including when DB is unset) so a
    missing DB doesn't silently disable navigation for the whole run."""
    conn = db()
    if conn is None or DB_LOCATION_ID is None:
        return True
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT voice_nav_enabled FROM locations WHERE location_id = %s", (DB_LOCATION_ID,))
            row = cur.fetchone()
            return bool(row[0]) if row else True
    except Exception as e:
        print(f"[warn] voice_nav_enabled check failed: {e}")
        return True


def navigate_and_wait(destination: str, timeout_s: float = NAV_TIMEOUT_S) -> bool:
    """Blocks until the robot has actually reached `destination` — a
    waypoint name from src/argo_mini/waypoints/<MAP_NAME>.json, e.g.
    "Kitchen", "Table 3", "Docker" — or the timeout elapses. Shells out to
    nav_bridge.py (the one piece of this file that talks to ROS/Nav2
    directly), sourcing the ROS environment the same way
    backend/launcher.py's /estop handler does from its own non-ROS process.
    Returns False without attempting anything if voice_nav_enabled() is
    off. Never raises — a nav failure is something callers decide how to
    react to (skip a conversation, apologize, retry), not a crash."""
    if not voice_nav_enabled():
        print(f"[nav] voice_nav_enabled is off — skipping trip to {destination!r}")
        return False

    cmd = (
        "source /opt/ros/humble/setup.bash && "
        f"source {REPO_ROOT}/install/setup.bash && "
        "export RMW_IMPLEMENTATION=rmw_cyclonedds_cpp && "
        f"python3 {shlex.quote(NAV_BRIDGE_SCRIPT)} --map {shlex.quote(MAP_NAME)} "
        f"--destination {shlex.quote(destination)} --timeout {timeout_s}"
    )
    print(f"[nav] -> {destination!r} (timeout {timeout_s:.0f}s)")
    try:
        result = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True, timeout=timeout_s + 15)
    except subprocess.TimeoutExpired:
        print(f"[nav] {destination!r}: bridge process itself timed out")
        return False

    ok = result.returncode == 0 and "RESULT:SUCCESS" in result.stdout
    print(f"[nav] {destination!r}: {'arrived' if ok else 'FAILED'} (rc={result.returncode})")
    if not ok and result.stderr:
        print(f"[nav] stderr: {result.stderr.strip()[-500:]}")
    return ok


def db_log_cutlery_request(state: DialogueState, items: list) -> None:
    conn = db()
    if conn is None or not state.db_service_point_id:
        return
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO cutlery_requests (service_point_id, visit_id) VALUES (%s, %s) RETURNING request_id",
                (state.db_service_point_id, state.db_visit_id),
            )
            request_id = cur.fetchone()[0]
            for item in items:
                if item not in VALID_CUTLERY:
                    continue
                cur.execute(
                    "INSERT INTO cutlery_request_items (request_id, item) VALUES (%s, %s) ON CONFLICT (request_id, item) DO NOTHING",
                    (request_id, item),
                )
    except Exception as e:
        print(f"[warn] DB cutlery request log failed: {e}")


def db_log_staff_call(state: DialogueState, target: str) -> None:
    conn = db()
    if conn is None or not state.db_service_point_id:
        return
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO staff_calls (service_point_id, visit_id, robot_id, call_target) VALUES (%s, %s, %s, %s)",
                (state.db_service_point_id, state.db_visit_id, DB_ROBOT_ID, target),
            )
    except Exception as e:
        print(f"[warn] DB staff call log failed: {e}")


# speak() looks up "whose turn is this" via this module-level reference
# rather than threading a state parameter through every call site. It's kept
# up to date by trace_node() (called as the first line of every graph node)
# and by run_graph_session() (for prompts spoken outside any node).
_current_state: Optional[DialogueState] = None


# ---------------------------------------------------------------------------
# Menu
# ---------------------------------------------------------------------------

# Populated by load_menu_from_db() (called from main() after resolve_robot(),
# once DB_LOCATION_ID is known) — empty until then, so nothing before that
# point in startup should rely on menu contents.
MENU_ITEMS: list = []
MENU_LOOKUP: dict = {}


def load_menu_from_db() -> None:
    """Loads the venue's menu from Postgres (menu_categories/menu_items,
    joined by DB_LOCATION_ID) into MENU_ITEMS/MENU_LOOKUP/MENU_CATEGORIES.
    Menu is DB-only now (no bundled menu.json fallback) — requires
    DATABASE_URL to be set and `seed_db.py` to have been run first."""
    global MENU_ITEMS, MENU_LOOKUP, MENU_CATEGORIES
    conn = db()
    if conn is None or DB_LOCATION_ID is None:
        print("No database/location available — menu can't be loaded. "
              "Set DATABASE_URL in .env, then run run_migrations.py and seed_db.py.")
        sys.exit(1)

    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT mi.menu_item_id, mi.item_name, mc.name, mi.price,
                          mi.description, mi.is_available
                   FROM menu_items mi
                   LEFT JOIN menu_categories mc ON mi.category_id = mc.category_id
                   WHERE mi.location_id = %s""",
                (DB_LOCATION_ID,),
            )
            rows = cur.fetchall()
            cur.execute(
                """SELECT menu_item_id, alias FROM menu_item_aliases
                   WHERE menu_item_id IN (SELECT menu_item_id FROM menu_items WHERE location_id = %s)""",
                (DB_LOCATION_ID,),
            )
            alias_rows = cur.fetchall()
    except Exception as e:
        print(f"Menu load from DB failed: {e}")
        sys.exit(1)

    if not rows:
        print(f"No menu_items found for location_id={DB_LOCATION_ID} — run seed_db.py first.")
        sys.exit(1)

    aliases_by_item: dict = {}
    for menu_item_id, alias in alias_rows:
        aliases_by_item.setdefault(str(menu_item_id), []).append(alias)

    items = []
    categories = set()
    for menu_item_id, item_name, category_name, price, description, is_available in rows:
        category = (category_name or "all").strip().lower().replace(" ", "_").replace("-", "_")
        categories.add(category)
        items.append({
            "item_id": str(menu_item_id),
            "name": item_name,
            "category": category,
            "price": float(price),
            "description": description,
            "available": is_available,
            "aliases": aliases_by_item.get(str(menu_item_id), []),
        })

    lookup = {}
    for item in items:
        lookup[item["name"].strip().lower()] = item
        for alias in item["aliases"]:
            lookup[alias.strip().lower()] = item

    MENU_ITEMS, MENU_LOOKUP, MENU_CATEGORIES = items, lookup, categories
    print(f"[db] Loaded {len(items)} menu items across {len(categories)} categories from Postgres")


def normalize_category(cat: Optional[str]) -> str:
    if not cat:
        return "all"
    key = cat.strip().lower().replace(" ", "_").replace("-", "_")
    return key if key in MENU_CATEGORIES else "all"


def find_menu_item(spoken_name: Optional[str]) -> Optional[dict]:
    if not spoken_name:
        return None
    key = spoken_name.strip().lower()
    if key in MENU_LOOKUP:
        return MENU_LOOKUP[key]
    matches = difflib.get_close_matches(key, MENU_LOOKUP.keys(), n=1, cutoff=0.72)
    return MENU_LOOKUP[matches[0]] if matches else None


_ITEM_SPLIT_RE = re.compile(r"\s*(?:,|&|\band\b)\s*", re.IGNORECASE)
_LEADING_ARTICLE_RE = re.compile(r"^(a|an|the)\s+", re.IGNORECASE)


def split_item_names(text: Optional[str]) -> list:
    """Splits a slot value like 'a paneer tikka and a crispy corn' into
    ['paneer tikka', 'crispy corn'] — the NLU sometimes returns multiple
    items named in one breath as a single comma/and-joined string."""
    if not text:
        return []
    parts = _ITEM_SPLIT_RE.split(text)
    cleaned = []
    for p in parts:
        p = _LEADING_ARTICLE_RE.sub("", p.strip()).strip()
        if p:
            cleaned.append(p)
    return cleaned


def menu_items_by_category(category: str) -> list:
    items = [i for i in MENU_ITEMS if i.get("available", True)]
    if category != "all":
        items = [i for i in items if i["category"] == category]
    return items


def get_item_price(order_item: dict) -> float:
    for m in MENU_ITEMS:
        if m["item_id"] == order_item["item_id"]:
            return m["price"]
    return 0.0


# ---------------------------------------------------------------------------
# Audio I/O
# ---------------------------------------------------------------------------

def record_utterance(timeout_s: float = SILENCE_ONSET_TIMEOUT_S) -> Optional[np.ndarray]:
    """Wait up to timeout_s for speech to start; once it does, record until
    ~TRAILING_SILENCE_MS of silence. Returns int16 mono PCM, or None if the
    user never spoke within the timeout window."""
    q: queue.Queue = queue.Queue()

    def callback(indata, frames, time_info, status):
        q.put(indata.copy())

    frames_collected = []
    speech_started = False
    silence_run_ms = 0
    start = time.monotonic()

    with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="int16",
                         blocksize=FRAME_SAMPLES, callback=callback):
        while True:
            try:
                chunk = q.get(timeout=0.5)
            except queue.Empty:
                if not speech_started and (time.monotonic() - start) > timeout_s:
                    return None
                continue

            rms = float(np.sqrt(np.mean(chunk.astype(np.float64) ** 2)))

            if not speech_started:
                if rms > SPEECH_RMS_THRESHOLD:
                    speech_started = True
                    frames_collected.append(chunk)
                elif (time.monotonic() - start) > timeout_s:
                    return None
                continue

            frames_collected.append(chunk)
            if rms > SPEECH_RMS_THRESHOLD:
                silence_run_ms = 0
            else:
                silence_run_ms += FRAME_MS
                if silence_run_ms >= TRAILING_SILENCE_MS:
                    break

    return np.concatenate(frames_collected, axis=0).flatten()


def play_audio(audio: np.ndarray, samplerate: int = SAMPLE_RATE) -> None:
    sd.play(audio, samplerate=samplerate)
    sd.wait()


# ---------------------------------------------------------------------------
# Sarvam AI: STT (saarika:v2.5) / TTS (bulbul:v3)
# ---------------------------------------------------------------------------

def sarvam_stt(audio: np.ndarray) -> str:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(audio.astype(np.int16).tobytes())
    buf.seek(0)

    resp = requests.post(
        SARVAM_STT_URL,
        headers={"api-subscription-key": SARVAM_API_KEY},
        data={"model": SARVAM_STT_MODEL, "language_code": SARVAM_LANGUAGE_CODE},
        files={"file": ("utterance.wav", buf, "audio/wav")},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    return (data.get("transcript") or data.get("text") or "").strip()


def sarvam_tts(text: str) -> tuple[np.ndarray, int]:
    resp = requests.post(
        SARVAM_TTS_URL,
        headers={
            "api-subscription-key": SARVAM_API_KEY,
            "Content-Type": "application/json",
        },
        json={
            "inputs": [text],
            "target_language_code": SARVAM_LANGUAGE_CODE,
            "model": SARVAM_TTS_MODEL,
            "speaker": SARVAM_SPEAKER,
            "pace": SARVAM_TTS_PACE,
        },
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    audios = data.get("audios") or []
    if not audios:
        raise RuntimeError(f"Sarvam TTS returned no audio: {data}")

    raw = base64.b64decode(audios[0])
    with wave.open(io.BytesIO(raw), "rb") as wf:
        sr = wf.getframerate()
        pcm = np.frombuffer(wf.readframes(wf.getnframes()), dtype=np.int16)
    return pcm, sr


def sarvam_tts_stream(sentences: list) -> bool:
    """Streams TTS audio for `sentences` over ONE Sarvam WebSocket
    connection (their real low-latency streaming API) and plays it through
    a SINGLE persistent sounddevice OutputStream, writing each chunk to it
    as it arrives — gapless, unlike play_audio()'s per-call
    sd.play()/sd.wait() (opens/closes the audio device every call; fine
    for the REST fallback's few large per-sentence buffers, but produced
    audible clicks/breaks once streaming's much smaller, much more
    frequent chunks went through that same per-call path).

    Returns True if playback happened (fully or partially — see
    `played_any` below). Returns False, having played NOTHING, on a
    failure before any audio arrived — missing `websockets` package,
    connection failure, protocol error on the first chunk — so the caller
    falls back to sarvam_tts()'s REST path cleanly from the start. If a
    failure happens mid-stream AFTER some audio already played, this
    returns True anyway (accepting the partial read) rather than False,
    since falling back at that point would replay the sentence(s) already
    heard. This hasn't been exercised against the live API from a dev
    sandbox; treat failures here as expected until verified on real
    hardware.

    Protocol (per Sarvam's docs, api.sarvam.ai/text-to-speech/ws):
      connect -> send {"type":"config","data":{...}} once
      -> send {"type":"text","data":{"text": sentence}} + {"type":"flush"}
         per sentence
      <- receive {"type":"audio","data":{"audio": base64}} chunks and
         {"type":"event","data":{"event_type":"final"}} once per sentence
         when that sentence's audio is fully sent."""
    try:
        import websockets.sync.client as ws_client
    except ImportError:
        return False

    played_any = False
    stream = None
    url = f"{SARVAM_TTS_WS_URL}?model={SARVAM_TTS_MODEL}&send_completion_event=true"
    try:
        with ws_client.connect(url, additional_headers={"Api-Subscription-Key": SARVAM_API_KEY}) as ws:
            ws.send(json.dumps({
                "type": "config",
                "data": {
                    "language_code": SARVAM_LANGUAGE_CODE,
                    "speaker": SARVAM_SPEAKER,
                    "model": SARVAM_TTS_MODEL,
                    "pace": SARVAM_TTS_PACE,
                    "speech_sample_rate": SARVAM_TTS_SAMPLE_RATE,
                    "output_audio_codec": "linear16",  # raw PCM — no extra codec decoding needed
                },
            }))
            for sentence in sentences:
                ws.send(json.dumps({"type": "text", "data": {"text": sentence}}))
                ws.send(json.dumps({"type": "flush"}))

            stream = sd.OutputStream(samplerate=SARVAM_TTS_SAMPLE_RATE, channels=1, dtype="int16")
            stream.start()

            pending = len(sentences)
            while pending > 0:
                msg = json.loads(ws.recv(timeout=20))
                mtype = msg.get("type")
                if mtype == "audio":
                    pcm = np.frombuffer(base64.b64decode(msg["data"]["audio"]), dtype=np.int16)
                    if pcm.size:
                        stream.write(pcm)
                        played_any = True
                elif mtype == "event" and msg.get("data", {}).get("event_type") == "final":
                    pending -= 1
                elif mtype == "error":
                    print(f"[warn] Sarvam streaming TTS error: {msg}")
                    return played_any
        return True
    except Exception as e:
        print(f"[warn] Sarvam streaming TTS {'ended early' if played_any else 'unavailable, falling back to REST'}: {e}")
        return played_any
    finally:
        if stream is not None:
            stream.stop()
            stream.close()


_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")


def split_sentences(text: str) -> list:
    parts = _SENTENCE_SPLIT_RE.split(text.strip())
    return [p for p in parts if p]


def speak(text: str) -> None:
    print(f"Sonic: {text}")
    if _current_state is not None:
        db_log_turn(_current_state, "robot", text)
    if TEXT_MODE:
        return

    sentences = split_sentences(text)
    if not sentences:
        return

    # Try Sarvam's real streaming API first — sarvam_tts_stream() owns its
    # own gapless playback (a single persistent OutputStream) and returns
    # True once it's actually played something, so there's nothing left
    # for this function to do. Falls through to the REST path below on any
    # failure (missing dependency, connection issue, protocol error)
    # rather than the robot going silently mute.
    if sarvam_tts_stream(sentences):
        return

    # Fallback: REST path, sentence-level pipelining — synthesize sentence
    # N+1 on a background thread while sentence N plays, instead of
    # waiting for the whole (possibly multi-sentence) reply to be
    # synthesized before any audio starts.
    audio_queue: "queue.Queue" = queue.Queue(maxsize=2)
    DONE = object()

    def synthesize_worker():
        for sentence in sentences:
            try:
                audio_queue.put(sarvam_tts(sentence))
            except Exception as e:
                print(f"[warn] TTS failed for a chunk: {e}")
        audio_queue.put(DONE)

    worker = threading.Thread(target=synthesize_worker, daemon=True)
    worker.start()

    while True:
        item = audio_queue.get()
        if item is DONE:
            break
        audio, sr = item
        try:
            play_audio(audio, sr)
        except Exception as e:
            print(f"[warn] audio playback failed: {e}")
    worker.join(timeout=1.0)


def listen(timeout_s: float = SILENCE_ONSET_TIMEOUT_S) -> Optional[str]:
    if TEXT_MODE:
        try:
            text = input("You: ").strip()
        except EOFError:
            print()
            sys.exit(0)
        if text.lower() in ("quit", "exit"):
            print("Goodbye!")
            sys.exit(0)
        return text or None

    audio = record_utterance(timeout_s)
    if audio is None:
        return None
    try:
        transcript = sarvam_stt(audio)
    except Exception as e:
        print(f"[warn] STT failed: {e}")
        return None
    print(f"You said: {transcript}")
    return transcript or None


def listen_with_reprompt(prompt_text: str) -> Optional[str]:
    """Speak prompt_text and wait for a reply; on silence, repeat once; on a
    second silence, return None so the caller can fall back to idle."""
    speak(prompt_text)
    reply = listen()
    if reply is not None:
        return reply
    speak(prompt_text)
    return listen()


# ---------------------------------------------------------------------------
# Wake word (openWakeWord + Hi_Sonic.onnx)
# ---------------------------------------------------------------------------

def load_wake_word_model():
    try:
        from openwakeword import Model
    except ImportError:
        from openwakeword.model import Model
    return Model(wakeword_models=[WAKE_WORD_MODEL_PATH], inference_framework="onnx")


def wait_for_wake_word(oww_model) -> None:
    oww_model.reset()
    q: queue.Queue = queue.Queue()

    def callback(indata, frames, time_info, status):
        q.put(indata.copy().flatten())

    with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="int16",
                         blocksize=1280, callback=callback):
        while True:
            chunk = q.get()
            predictions = oww_model.predict(chunk)
            score = predictions.get(WAKE_WORD_NAME)
            if score is None and predictions:
                score = next(iter(predictions.values()))
            if score is not None and score > WAKE_WORD_THRESHOLD:
                return


# ---------------------------------------------------------------------------
# Groq LLM: NLU (intent + slots) and free-form response generation
# ---------------------------------------------------------------------------

class GroqJSONError(RuntimeError):
    """Groq validated our response_format=json_object request and rejected
    the model's own output for not actually being valid JSON. Carries the
    raw (broken) generation so the caller can show it back to the model to
    self-correct, instead of just discarding it."""
    def __init__(self, message: str, failed_generation: Optional[str] = None):
        super().__init__(message)
        self.failed_generation = failed_generation


def call_groq(messages: list, *, json_mode: bool = False, temperature: float = 0.2) -> str:
    payload = {"model": GROQ_MODEL, "messages": messages, "temperature": temperature}
    if json_mode:
        payload["response_format"] = {"type": "json_object"}

    last_err = None
    for attempt in range(2):
        # nudge temperature up on a retry so a near-deterministic malformed
        # generation (low temperature) isn't just reproduced identically
        payload["temperature"] = temperature + (0.15 if attempt > 0 else 0.0)
        try:
            resp = requests.post(
                GROQ_URL,
                headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
                json=payload,
                timeout=30,
            )
            if not resp.ok:
                # surface Groq's actual error body (400s especially carry a
                # specific reason) instead of requests' generic "Bad Request"
                body_text = resp.text[:2000]
                failed_generation = None
                try:
                    failed_generation = resp.json().get("error", {}).get("failed_generation")
                except Exception:
                    pass
                if failed_generation:
                    raise GroqJSONError(f"{resp.status_code} {resp.reason}: {body_text[:500]}",
                                         failed_generation=failed_generation)
                raise RuntimeError(f"{resp.status_code} {resp.reason}: {body_text[:500]}")
            return resp.json()["choices"][0]["message"]["content"]
        except Exception as e:
            last_err = e
            time.sleep(0.5)
    if isinstance(last_err, GroqJSONError):
        raise last_err
    raise RuntimeError(f"Groq call failed after retries: {last_err}")


INTENT_DESCRIPTIONS = """
Valid top-level intents:
- menu: user wants to browse/hear the menu. slots: menu_category (one of: starters, soups,
  salads, snacks, main_course, breads, desserts, drinks, beverages, specials, or "all")
- call_people: user wants to call staff. slots: call_target (one of: manager, waiter, cleaner)
- take_order: user wants to place/edit/cancel/add to an order. slots:
    order_action (one of: new_order, edit_order, cancel_order, add_item)
    item_name (free text dish name), quantity (integer), preference (free text, e.g. "no onion")
    edit_action (one of: replace, customize, cancel_single, cancel_full) -- only when order_action=edit_order
- navigation: user wants the robot to go somewhere. slots: location (one of: docker, kitchen, table)
- cutlery: user wants cutlery delivered. slots: cutlery_items (list from: spoon, fork, glass, knife, napkin)
- about: user is asking about the robot itself (not placing a request). slots: about_topic:
    who_are_you       -- "who are you", "what's your name"
    what_can_you_do   -- "what can you do", "what services do you offer", "how can you help"
    about_robot       -- "how do you work", "are you an AI", "what powers you"
    famous_for        -- "what are you known/famous for", "why should I use you"
    other             -- anything else about the robot, INCLUDING questions about its current
                          physical location/position ("where are you right now", "are you
                          nearby") — this system has no live position tracking, so route
                          those to "other" rather than defaulting to what_can_you_do
- normal_conv: open-ended small talk, not matching any of the above
- get_bill: user wants their bill / check

Additionally, ALWAYS extract this slot regardless of intent, if mentioned anywhere in the
utterance: table_no (the guest's table number, as a string, e.g. "5").
""".strip()


def build_nlu_system_prompt(state: DialogueState) -> str:
    pending = state.pending_question or "none — this is a fresh top-level request"
    order_summary = json.dumps(state.current_order, ensure_ascii=False) if state.current_order else "empty"
    return f"""You are the natural-language-understanding module for "Sonic", a restaurant
service robot. Read the user's latest utterance and return ONLY a JSON object (no prose, no
markdown fences) with exactly this shape:

{{
  "intent": "<one of: menu, call_people, take_order, navigation, cutlery, about, normal_conv, get_bill, or null>",
  "intent_switched": <true/false>,
  "answers_pending_question": <true/false>,
  "slots": {{}},
  "yes_no": "<yes, no, or null>",
  "response_text": "<a short, warm, natural spoken reply — only for normal_conv terminal answers
    (about/menu/get_bill use their own fixed replies, leave this empty for those), else empty string.
    Never wrap it in quotation marks — plain spoken text only, not quoted dialogue.>",
  "clarification_needed": <true/false>,
  "clarification_text": "<short clarifying question, or null — see strict rule below>"
}}

IMPORTANT — clarification_needed is a LAST RESORT, not a general way to ask a follow-up
question: set it to true ONLY if the utterance is so unintelligible or unrelated to anything
this robot does that you genuinely cannot pick any intent at all (e.g. pure noise, gibberish).
If the utterance relates to placing/editing/cancelling an order in ANY way — even vaguely,
even with the item or quantity unspecified — set intent="take_order" with whatever slots you
can confidently extract (possibly none) and clarification_needed=false. The structured order
flow (not you) will ask the guest for whatever specific detail is still missing, one question
at a time — that is not your job. The same applies to the other intents: pick your best-guess
intent over asking a clarifying question whenever there is any reasonable signal at all.

{INTENT_DESCRIPTIONS}

Current dialogue context:
- Active flow / intent right now: {state.active_intent or "none, robot is idle and just greeted the user"}
- The robot just asked the user: {pending}
- Order so far (already-committed line items, not counting anything mid-entry): {order_summary}

Rules:
- If the user's utterance plainly answers the pending question above (a number for a quantity,
  a yes/no, an item name, a preference), set answers_pending_question=true, intent_switched=false,
  and keep intent equal to the active flow's intent.
- Specifically for "which dish" pending questions: only set answers_pending_question=true if you
  can actually extract a concrete item_name. Garbled, unclear, or off-topic speech (including
  mis-transcribed audio that doesn't name any dish) must get answers_pending_question=false, even
  if intent stays "take_order" — don't guess at an item_name that isn't really there.
- Only set intent_switched=true if the utterance is clearly about a different top-level topic
  than the active flow (e.g. mid-order the user asks for the bill, asks to see the menu, or asks
  to call a waiter). A vague, short, or ambiguous reply should NOT be treated as a switch — prefer
  answers_pending_question=true in ambiguous cases.
- Treat informal affirmatives ("yeah", "ya", "yep", "sure", "yup", "go ahead") as yes_no="yes",
  and informal negatives ("nah", "nope", "not now") as yes_no="no" — do not require the literal
  words "yes"/"no".
- If the pending question is a yes/no question but the user's reply ALSO directly supplies the
  next piece of information in the same breath (e.g. "yeah, I'd like a veg burger" while being
  asked "would you like to add anything else?"), set yes_no accordingly AND still fill in any
  slots you can extract (e.g. item_name) — don't discard extra information just because a yes/no
  was also given.
- If the pending question is "would you like to add anything else?" and the user names (or asks
  about) a new item, dish, or category instead of literally saying yes ("I want some drinks too",
  "can I get a coke", "add a naan"), infer yes_no="yes" — naming something to add IS the answer;
  still extract whatever item_name/item_category slots you can from it.
- If the utterance asks to repeat, read back, or recap the order so far, do not set yes_no; leave
  it null and leave clarification_needed=false (this is handled separately, not by you).
- If the pending question is about confirming, placing, or keeping an order (e.g. "shall I
  confirm the order?", "would you like to add anything else?"), treat ANY clear expression of
  declining, cancelling, changing their mind, or wanting to redo it — however phrased (e.g. "wait,
  cancel this, I'll order again", "actually never mind", "scratch that") — as yes_no="no". Don't
  require the literal word "no"; infer the negative intent.
- If the utterance requests multiple things at once (e.g. "bring water and show me the menu, then
  I'll order"), pick the single most actionable intent for right now — prefer take_order if the
  user explicitly says they want to order; otherwise prefer whichever request they'd naturally
  expect handled first — and still extract any slots (like table_no) mentioned for the parts you
  didn't pick, since those may be reused later.
- If generating response_text (normal_conv only): be warm and human — if the guest makes small
  talk or asks how you're doing, respond to it briefly and naturally before steering back to
  helping them, rather than ignoring it. Never wrap response_text in quotation marks.
- Only fill slot keys relevant to the detected intent / pending question.
- quantity must be a positive integer if present.
- Return valid JSON only, no other text. Every string value — including response_text and
  clarification_text — MUST be wrapped in double quotes, e.g. "response_text": "like this",
  never "response_text": like this without quotes.
"""


def run_nlu(transcript: str, state: DialogueState) -> dict:
    system_prompt = build_nlu_system_prompt(state)
    history_tail = state.conversation_history[-6:]
    history_text = "\n".join(f"{t['role']}: {t['text']}" for t in history_tail)
    user_prompt = f"Recent conversation:\n{history_text}\n\nLatest user utterance: {transcript}"
    messages = [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}]

    result = {}
    nlu_failed = False
    try:
        raw = call_groq(messages, json_mode=True, temperature=0.1)
        result = json.loads(raw)
    except GroqJSONError as e:
        # Groq itself rejected the model's output as invalid JSON (e.g. a
        # string value left unquoted) — show the model its own broken output
        # and ask it to fix it, rather than discarding the whole turn.
        print(f"[warn] NLU produced invalid JSON, asking it to correct itself: {e}")
        try:
            fix_messages = messages + [
                {"role": "assistant", "content": e.failed_generation or ""},
                {"role": "user", "content": "That was not valid JSON. Every string value — including "
                                             "response_text and clarification_text — must be wrapped in "
                                             "double quotes. Return the corrected JSON object only, no "
                                             "other text."},
            ]
            raw = call_groq(fix_messages, json_mode=True, temperature=0.3)
            result = json.loads(raw)
        except Exception as e2:
            print(f"[warn] NLU self-correction also failed: {e2}")
            result = {}
            nlu_failed = True
    except Exception as e:
        print(f"[warn] NLU call/parse failed: {e}")
        result = {}
        nlu_failed = True

    # a genuine API/parse failure must NEVER be papered over by a generated
    # conversational reply -- that risks hallucinating something like "I'll
    # get that order right out to you" when nothing was actually understood
    # or acted on. Force an honest, generic response_text so every downstream
    # consumer (normal_conv_handler, the off-topic-aside helpers) uses this
    # instead of asking the LLM to improvise a reply to the raw transcript.
    response_text = ("Sorry, I'm having a little trouble understanding right now — could you say that again?"
                      if nlu_failed else clean_spoken_text(result.get("response_text")))

    return {
        "intent": result.get("intent"),
        "intent_switched": bool(result.get("intent_switched", False)),
        "answers_pending_question": bool(result.get("answers_pending_question", False)),
        "slots": result.get("slots") or {},
        "yes_no": result.get("yes_no"),
        "response_text": response_text,
        "clarification_needed": bool(result.get("clarification_needed", False)),
        "clarification_text": clean_spoken_text(result.get("clarification_text")),
        "nlu_failed": nlu_failed,
    }


_WRAPPING_QUOTES = '"\'“”‘’'


def clean_spoken_text(text: Optional[str]) -> str:
    """LLMs sometimes wrap a generated reply in quotation marks like it's
    quoting dialogue — strip that so it doesn't get spoken/printed verbatim
    with visible quote characters. Also capitalizes the first letter, since
    the model sometimes returns an otherwise-fine sentence in lowercase."""
    if not text:
        return ""
    cleaned = text.strip().strip(_WRAPPING_QUOTES).strip()
    if cleaned and cleaned[0].isalpha():
        cleaned = cleaned[0].upper() + cleaned[1:]
    return cleaned


def seeded_outcome(**slots) -> dict:
    """Builds a normalized NLU-result-shaped dict (matching run_nlu's return
    shape) for internally-queued follow-ups (e.g. a second item named in the
    same breath) that don't need a real NLU call to process."""
    return {
        "intent": "take_order", "intent_switched": False, "answers_pending_question": True,
        "slots": slots, "yes_no": None, "response_text": "",
        "clarification_needed": False, "clarification_text": None,
    }


CONVERSATIONAL_PERSONA = (
    "You are Sonic, a warm and genuinely friendly restaurant service robot with good manners "
    "— think of a helpful, attentive human host, not a script-reading machine. If the guest makes "
    "small talk (e.g. asks how your day is going, cracks a joke), respond to it briefly and "
    "naturally in kind before gently steering back to helping them, instead of ignoring it and "
    "jumping straight to business. Use warm, polite, natural phrasing (\"sure thing\", \"of "
    "course\", \"happy to help\") rather than stiff or robotic wording. Keep replies to 1-2 "
    "sentences since they're spoken aloud. Never wrap your reply in quotation marks — write it as "
    "plain spoken text, not as a quoted line of dialogue."
)


def generate_response(prompt: str, *, temperature: float = 0.6) -> str:
    try:
        text = call_groq(
            [
                {"role": "system", "content": CONVERSATIONAL_PERSONA},
                {"role": "user", "content": prompt},
            ],
            temperature=temperature,
        )
        return clean_spoken_text(text)
    except Exception as e:
        print(f"[warn] response generation failed: {e}")
        return "Sorry, I'm having a little trouble right now."


# ---------------------------------------------------------------------------
# LangGraph plumbing: interrupt-based ask/timeout, node tracing, shared
# outcome-processing helpers used by every ask_* / edit_* / cancel_* node
# ---------------------------------------------------------------------------

TIMEOUT_SENTINEL = "__SONIC_TIMEOUT__"


def trace_node(name: str, state: DialogueState) -> None:
    global _current_state
    _current_state = state
    trace(f"[langgraph node] {name}", **{"order": order_snapshot(state)})


def interpret(state: DialogueState, transcript: str) -> dict:
    """Log the user turn, run NLU, and apply any ambient slots (currently
    just table_no) regardless of what intent/question this call was about."""
    state.conversation_history.append({"role": "user", "text": transcript})
    nlu = run_nlu(transcript, state)
    trace("interpret: NLU result", transcript=transcript, intent=nlu.get("intent"),
          intent_switched=nlu.get("intent_switched"), answers_pending=nlu.get("answers_pending_question"),
          slots=nlu.get("slots"), yes_no=nlu.get("yes_no"))
    db_log_turn(state, "user", transcript, intent=nlu.get("intent"), slots=nlu.get("slots"))
    table_no = nlu.get("slots", {}).get("table_no")
    if table_no and not state.table_no_locked:
        # only ever accept the FIRST table_no capture for this visit — once
        # locked, a later misheard number (STT garble) can't silently
        # redirect an in-progress order/visit to the wrong table
        state.table_no = str(table_no)
        state.table_no_locked = True
        resolve_visit(state)
    return nlu


FAREWELL_PHRASES = (
    "bye", "goodbye", "good bye", "that's all", "thats all", "that is all",
    "nothing else", "no thanks", "no thank you", "i'm good", "im good",
    "we're good", "were good", "that's it", "thats it",
)


def is_farewell_text(text: str) -> bool:
    t = text.strip().lower()
    return any(t == p or t.startswith(p + " ") or t.startswith(p + ",") for p in FAREWELL_PHRASES)


REPEAT_ORDER_PHRASES = ("repeat", "read back", "read out", "what did i order", "what's my order", "whats my order")

ORDER_SLOT_QUESTIONS = {
    "ask_item": "what item you'd like",
    "ask_qty": "how many you'd like",
    "ask_preference": "any preference for the item",
    "ask_to_add": "whether to add anything else",
    "confirm_order": "whether to confirm the order",
}

INTENT_TO_NODE = {
    "menu": "menu_node", "call_people": "call_people_node", "navigation": "navigation_node",
    "cutlery": "cutlery_node", "about": "about_node", "get_bill": "get_bill_node",
    "normal_conv": "normal_conv_node", "take_order": "new_order_start",
}


def route_intent_to_node(intent: Optional[str]) -> str:
    return INTENT_TO_NODE.get(intent, "normal_conv_node")


def get_outcome_or_route(state: DialogueState, prompt: str, pending_question: str, this_node: str):
    """Shared logic for every free-text ask_*/edit_*/cancel_* node: get an
    NLU outcome (from a queued seed or a fresh interrupt+listen), and check
    it for a timeout / intent switch / off-topic tangent. Returns the outcome
    dict if the node should keep processing it, or None if state.next_node
    was already set (caller should just `return asdict(state)` immediately)."""
    if state.pending_seed is not None:
        outcome = state.pending_seed
        state.pending_seed = None
    else:
        answer = interrupt({"prompt": prompt})
        if answer == TIMEOUT_SENTINEL:
            state.went_idle = True
            state.next_node = "respond"
            return None
        state.pending_question = pending_question
        outcome = interpret(state, answer)

    # a genuine switch to another actionable intent (menu, get_bill, cutlery,
    # call_people, navigation...) is worth pausing the order for. "normal_conv"
    # is just small talk/chit-chat (e.g. "hello") -- not worth derailing the
    # order for, so it falls through to the off-topic-aside handling below
    # instead, which answers briefly and picks the SAME question right back up.
    if (outcome.get("intent_switched") and outcome.get("intent")
            and outcome.get("intent") not in (state.active_intent, "normal_conv")):
        state.last_outcome = outcome
        state.next_node = "handle_switch"
        return None

    if not outcome.get("answers_pending_question"):
        trace("off-topic tangent detected", node=this_node)
        last_said = state.conversation_history[-1]["text"] if state.conversation_history else ""
        aside = outcome.get("response_text") or generate_response(
            f'You are a friendly restaurant waiter mid-order. You just asked the guest: "{prompt}". '
            f'Instead of answering, they said: "{last_said}". Respond briefly and warmly — if they seem '
            f"unsure or ask for a suggestion, offer one — you'll repeat your question right after this."
        )
        if aside:
            speak(aside)
        state.next_node = this_node  # retry: re-enter this same node
        return None

    return outcome


def get_yes_no_or_route(state: DialogueState, prompt: str, this_node: str):
    """Same idea as get_outcome_or_route but for yes/no questions: handles
    the repeat-order-summary aside and the "was that a yes or no?" ambiguous
    case too. Returns True/False if we got a clear answer (also stashed on
    state.last_outcome), or None if next_node was already set."""
    if state.pending_seed is not None:
        outcome = state.pending_seed
        state.pending_seed = None
    else:
        answer = interrupt({"prompt": prompt})
        if answer == TIMEOUT_SENTINEL:
            state.went_idle = True
            state.next_node = "respond"
            return None
        state.pending_question = f"yes/no: {prompt}"
        outcome = interpret(state, answer)

    # see get_outcome_or_route: chit-chat ("hello") isn't worth pausing the
    # order for -- answer it briefly and re-ask the same yes/no question
    if (outcome.get("intent_switched") and outcome.get("intent")
            and outcome.get("intent") not in (state.active_intent, "normal_conv")):
        state.last_outcome = outcome
        state.next_node = "handle_switch"
        return None

    if outcome.get("intent") == "normal_conv" and not outcome.get("yes_no"):
        trace("off-topic chit-chat detected", node=this_node)
        last_said = state.conversation_history[-1]["text"] if state.conversation_history else ""
        aside = outcome.get("response_text") or generate_response(
            f'You are a friendly restaurant waiter mid-order. You just asked the guest: "{prompt}". '
            f'Instead of answering, they said: "{last_said}". Respond briefly and warmly — '
            f"you'll repeat your question right after this."
        )
        if aside:
            speak(aside)
        state.next_node = this_node
        return None

    yn = outcome.get("yes_no")
    if yn in ("yes", "no"):
        state.last_outcome = outcome
        return yn == "yes"

    last_text = state.conversation_history[-1]["text"].lower() if state.conversation_history else ""
    if any(p in last_text for p in REPEAT_ORDER_PHRASES):
        if state.current_order:
            lines = ", ".join(
                f"{i['quantity']}x {i['name']}" + ("" if i.get("confirmed") else " (not yet confirmed)")
                for i in state.current_order
            )
            speak(f"So far you have: {lines}.")
        else:
            speak("You don't have any items yet.")
        state.next_node = this_node
        return None

    speak("Sorry, was that a yes or a no?")
    state.next_node = this_node
    return None


def dispatch_robot_action(action: dict) -> None:
    print(f"[robot_action] {action}")


def perform_travel_action(announce_text: str, destination: str,
                           arrival_text: Optional[str] = None,
                           timeout_s: float = NAV_TIMEOUT_S) -> bool:
    """Speaks the pre-departure line, then blocks on a REAL Nav2 trip via
    navigate_and_wait() — the robot only speaks the arrival line once Nav2
    has actually confirmed arrival, never on a timer. Returns whether the
    trip succeeded, so callers can skip whatever was going to happen next
    (e.g. don't greet a table the robot never reached)."""
    speak(announce_text)
    arrived = navigate_and_wait(destination, timeout_s=timeout_s)
    if not arrived:
        speak("I'm having trouble getting there — could someone give me a hand?")
        return False
    if arrival_text:
        speak(arrival_text)
    return True


# ---------------------------------------------------------------------------
# Simple (single-turn) intent handlers — pure logic, called from their node
# ---------------------------------------------------------------------------

def menu_handler(state: DialogueState, slots: dict) -> None:
    category = normalize_category(slots.get("menu_category"))
    items = menu_items_by_category(category)
    if not items:
        speak(f"Sorry, I don't have anything in {category.replace('_', ' ')} right now.")
        return
    sample = random.sample(items, min(len(items), random.choice([3, 4])))
    names = ", ".join(i["name"] for i in sample)
    label = "our menu" if category == "all" else f"our {category.replace('_', ' ')}"
    speak(f"A few options from {label}: {names}. Please check the menu card for the full list and prices.")


def call_people_handler(state: DialogueState, slots: dict) -> None:
    target = slots.get("call_target")
    target = target if target in VALID_CALL_TARGETS else "waiter"
    speak(f"Calling the {target} now. They'll be with you shortly.")
    dispatch_robot_action({"type": "call_person", "target": target})
    db_log_staff_call(state, target)


def navigation_handler(state: DialogueState, slots: dict) -> None:
    if not voice_nav_enabled():
        speak("I can't move around right now — a staff member can help you with that.")
        return
    location = slots.get("location") or "table"
    if location == "docker":
        perform_travel_action("Heading to the docking station now.", "Docker")
    elif location == "kitchen":
        perform_travel_action("Heading to the kitchen now.", "Kitchen")
    else:
        table = state.table_no or "your table"
        destination = f"Table {state.table_no}" if state.table_no else "Table 1"
        perform_travel_action(
            f"Heading to {table} now, I'll be right there!",
            destination,
            arrival_text="Hello! I'm your robot assistant — would you like to order something?",
        )


def cutlery_handler(state: DialogueState, slots: dict) -> None:
    raw_items = slots.get("cutlery_items") or []
    if isinstance(raw_items, str):
        raw_items = [raw_items]
    items = [i.strip().lower() for i in raw_items if isinstance(i, str) and i.strip().lower() in VALID_CUTLERY]
    if not items:
        items = ["cutlery"]
    speak(f"Bringing: {', '.join(items)}.")
    dispatch_robot_action({"type": "deliver_cutlery", "items": items})
    db_log_cutlery_request(state, items)


ABOUT_ANSWERS = {
    "who_are_you": "I'm Sonic, your restaurant service robot.",
    "what_can_you_do": "I can take orders, show the menu, deliver cutlery, call staff, guide you around, and get your bill.",
    "about_robot": "I'm powered by an LLM-based conversational system with real-time speech.",
    "famous_for": "I'm known for fast, friendly table service.",
    "other": "I'm right here at your service! I can help with your order, the menu, "
             "cutlery, calling staff, or your bill — just ask.",
}


def about_handler(state: DialogueState, slots: dict) -> None:
    topic = slots.get("about_topic") or "other"
    speak(ABOUT_ANSWERS.get(topic, ABOUT_ANSWERS["other"]))


def get_bill_handler(state: DialogueState) -> None:
    total = db_get_bill_total(state)
    if total is None:
        total = state.bill_total
    speak(f"Your bill total is Rs. {total:.2f}.")


def normal_conv_handler(state: DialogueState, nlu: Optional[dict]) -> None:
    text = (nlu or {}).get("response_text")
    if not text:
        last_said = state.conversation_history[-1]["text"] if state.conversation_history else ""
        text = generate_response(f'The guest just said: "{last_said}". Reply to it naturally.')
    speak(text)


def find_order_line(state: DialogueState, item_name: Optional[str]) -> Optional[dict]:
    if not item_name:
        return None
    match = find_menu_item(item_name)
    target_name = match["name"] if match else item_name
    for line in state.current_order:
        if line["name"].lower() == target_name.lower():
            return line
    return None


def place_order(state: DialogueState) -> None:
    """Bills/sends only the items not yet confirmed, then marks them
    confirmed — current_order itself is never cleared here, so earlier items
    (already sent) stay visible alongside anything ordered after."""
    new_items = [i for i in state.current_order if not i.get("confirmed")]
    if not new_items:
        speak("There's nothing new to confirm right now.")
        return
    trace("place_order: sending NEW items to kitchen", new_items=[i["name"] for i in new_items])
    total = sum(get_item_price(i) * i["quantity"] for i in new_items)
    state.bill_total += total
    order_lines = ", ".join(f"{i['quantity']}x {i['name']}" for i in new_items)
    db_place_order(state, new_items, total)
    for i in new_items:
        i["confirmed"] = True
    speak(f"Order confirmed and sent to the kitchen: {order_lines}.")
    speak("Thank you for using our service!")


# ---------------------------------------------------------------------------
# Graph nodes — each mirrors one node of the original flowchart (graph.py).
# Every node calls trace_node() first (visible in the terminal), does its
# work (possibly pausing via interrupt() to ask the guest something), and
# sets state.next_node before returning asdict(state) — the conditional edge
# on every node just reads state.next_node to decide where to go next.
# ---------------------------------------------------------------------------

MAX_CLARIFICATIONS = 3


def n_intent_classify(state: DialogueState) -> dict:
    trace_node("intent_classify", state)

    if state.active_item is not None:
        item_name = state.active_item.get("name", "that item")
        prompt = f"Welcome back! We were in the middle of ordering {item_name} — want to continue with that?"
        answer = interrupt({"prompt": prompt})
        if answer == TIMEOUT_SENTINEL:
            speak("No worries, I'll be here if you need me. Just say Hi Sonic!")
            state.went_idle = True
            state.next_node = "respond"
            return asdict(state)
        if is_farewell_text(answer):
            speak("No problem, I've dropped that. Have a great day!")
            state.active_item = None
            state.order_action = None
            state.order_slot = None
            state.went_idle = True
            state.next_node = "respond"
            return asdict(state)
        next_q_hint = ORDER_SLOT_QUESTIONS.get(state.order_slot, "your order")
        state.pending_question = (
            f"yes/no: {prompt} If they say yes and also directly answer the next "
            f"order question in the same breath ({next_q_hint}), extract that into slots too. "
            f"If they're asking about something unrelated instead, classify their actual intent."
        )
        nlu = interpret(state, answer)
        state.first_turn = False

        # the intent detector: same topic (continuing the item) -> resume;
        # a genuinely different topic -> go handle it, but remember to offer
        # to come back to this item afterward instead of silently losing it
        if nlu.get("yes_no") == "yes":
            state.active_intent = "take_order"
            state.pending_seed = nlu
            state.next_node = state.order_slot or "ask_item"
            return asdict(state)
        if nlu.get("intent") == "normal_conv" and nlu.get("yes_no") != "no":
            # chit-chat ("hello") isn't worth dropping the item for -- answer
            # briefly and re-ask the same resume question, item still intact
            last_said = state.conversation_history[-1]["text"] if state.conversation_history else ""
            aside = nlu.get("response_text") or generate_response(
                f'You are a friendly restaurant waiter. You just asked the guest: "{prompt}". '
                f'Instead of answering, they said: "{last_said}". Respond briefly and warmly — '
                f"you'll ask again right after this."
            )
            if aside:
                speak(aside)
            state.next_node = "intent_classify"
            return asdict(state)
        if nlu.get("intent") and nlu.get("intent") != "take_order" and nlu.get("yes_no") != "no":
            state.paused_order = {
                "order_action": state.order_action,
                "order_slot": state.order_slot,
                "active_item": state.active_item,
            }
            state.pending_resume_after_switch = True
            state.active_item = None
            state.last_outcome = nlu
            state.active_intent = nlu["intent"]
            state.next_node = route_intent_to_node(nlu["intent"])
            return asdict(state)
        # explicit "no" or nothing concrete -- drop the unfinished item and
        # continue into the normal top-level flow below
        speak("No problem, I've dropped that.")
        state.active_item = None
        state.order_action = None
        state.order_slot = None

    if state.current_order:
        summary = ", ".join(
            f"{i['quantity']}x {i['name']}" + ("" if i.get("confirmed") else " (not yet confirmed)")
            for i in state.current_order
        )
        prompt = f"Welcome back! Your order so far: {summary}. Want to add more, confirm it, or something else?"
    elif not state.first_turn:
        prompt = "Anything else I can help with?"
    else:
        prompt = "Hi! How can I help you today?"

    answer = interrupt({"prompt": prompt})
    if answer == TIMEOUT_SENTINEL:
        speak("No worries, I'll be here if you need me. Just say Hi Sonic!")
        state.went_idle = True
        state.next_node = "respond"
        return asdict(state)

    if not state.first_turn and is_farewell_text(answer):
        speak("Sounds good — just say Hi Sonic if you need anything else!")
        state.went_idle = True
        state.next_node = "respond"
        return asdict(state)

    state.pending_question = "top-level: how can I help you"
    nlu = interpret(state, answer)

    clarifications = 0
    while nlu.get("clarification_needed") and nlu.get("clarification_text"):
        clarifications += 1
        if clarifications > MAX_CLARIFICATIONS:
            speak("Sorry, I'm still not following. Let's try again in a moment.")
            state.went_idle = True
            state.next_node = "respond"
            return asdict(state)
        answer2 = interrupt({"prompt": nlu["clarification_text"]})
        if answer2 == TIMEOUT_SENTINEL:
            speak("No worries, I'll be here if you need me. Just say Hi Sonic!")
            state.went_idle = True
            state.next_node = "respond"
            return asdict(state)
        state.pending_question = "top-level: clarifying the request"
        nlu = interpret(state, answer2)

    state.first_turn = False
    intent = nlu.get("intent") or "normal_conv"
    state.active_intent = intent
    state.last_outcome = nlu
    state.next_node = route_intent_to_node(intent)
    return asdict(state)


def n_handle_switch(state: DialogueState) -> dict:
    trace_node("handle_switch", state)
    nlu = state.last_outcome or {}
    new_intent = nlu.get("intent")

    was_ordering = state.active_intent == "take_order"
    if was_ordering:
        state.paused_order = {
            "order_action": state.order_action,
            "order_slot": state.order_slot,
            "active_item": state.active_item,
        }
        trace("paused order for switch", paused_order=state.paused_order, **{"order": order_snapshot(state)})
        speak("Sure, one moment.")
    state.pending_resume_after_switch = was_ordering

    state.active_intent = new_intent
    state.next_node = route_intent_to_node(new_intent)
    return asdict(state)


def n_respond(state: DialogueState) -> dict:
    trace_node("respond", state)

    if state.went_idle:
        state.active_intent = None
        state.pending_question = None
        # if there's an item mid-build (name picked, quantity/preference not
        # yet given), keep order_action/order_slot/active_item so the next
        # wake-word activation can offer to resume it instead of silently
        # dropping the item the guest already named
        if state.active_item is None:
            state.order_action = None
            state.order_slot = None
        state.item_attempts = 0
        state.pending_seed = None
        db_end_session(state)
        print("--- back to idle, listening for the wake word ---\n")
        state.next_node = "END"
        return asdict(state)

    if state.pending_resume_after_switch:
        saved = state.paused_order
        if saved:
            prompt = "Would you like to continue your order?"
            next_q_hint = ORDER_SLOT_QUESTIONS.get(saved["order_slot"], "your order")
            answer = interrupt({"prompt": prompt})
            if answer == TIMEOUT_SENTINEL:
                state.pending_resume_after_switch = False
                state.paused_order = None
                state.went_idle = True
                state.next_node = "respond"
                return asdict(state)
            state.active_intent = "take_order"  # restore context before interpreting
            state.pending_question = (
                f"yes/no: {prompt} If they say yes and also directly answer the next "
                f"order question in the same breath ({next_q_hint}), extract that into slots too. "
                f"If they're asking about something unrelated instead, classify their actual intent."
            )
            nlu = interpret(state, answer)

            # the intent detector: same topic (continuing the paused order) ->
            # resume it; a genuinely different topic -> go handle that instead,
            # but KEEP the pause so we ask to resume again once it's done
            if nlu.get("yes_no") == "yes":
                state.pending_resume_after_switch = False
                state.paused_order = None
                state.order_action = saved["order_action"]
                state.order_slot = saved["order_slot"]
                state.active_item = saved["active_item"]
                state.pending_seed = nlu
                state.next_node = saved["order_slot"]
                return asdict(state)
            if nlu.get("intent") == "normal_conv" and nlu.get("yes_no") != "no":
                # chit-chat ("hello") isn't worth derailing for -- answer
                # briefly and re-ask the same continue-your-order question,
                # keeping the pause intact
                last_said = state.conversation_history[-1]["text"] if state.conversation_history else ""
                aside = nlu.get("response_text") or generate_response(
                    f'You are a friendly restaurant waiter. You just asked the guest: "{prompt}". '
                    f'Instead of answering, they said: "{last_said}". Respond briefly and warmly — '
                    f"you'll ask again right after this."
                )
                if aside:
                    speak(aside)
                state.next_node = "respond"
                return asdict(state)  # pending_resume_after_switch/paused_order stay set
            if nlu.get("intent") and nlu.get("intent") != "take_order" and nlu.get("yes_no") != "no":
                state.last_outcome = nlu
                state.active_intent = nlu["intent"]
                state.next_node = route_intent_to_node(nlu["intent"])
                return asdict(state)  # pending_resume_after_switch/paused_order stay set

            # explicit "no" or nothing concrete -- give up on resuming
            state.pending_resume_after_switch = False
            state.paused_order = None
            # declined -- fall through to normal "anything else" handling
        else:
            state.pending_resume_after_switch = False

    state.active_intent = None
    state.pending_question = None
    state.order_action = None
    state.order_slot = None
    state.active_item = None
    state.item_attempts = 0
    state.pending_seed = None
    state.next_node = "intent_classify"
    return asdict(state)


# --- simple (single-turn) intent nodes -------------------------------------

def n_menu(state: DialogueState) -> dict:
    trace_node("menu_node", state)
    menu_handler(state, (state.last_outcome or {}).get("slots", {}))
    state.next_node = "respond"
    return asdict(state)


def n_call_people(state: DialogueState) -> dict:
    trace_node("call_people_node", state)
    call_people_handler(state, (state.last_outcome or {}).get("slots", {}))
    state.next_node = "respond"
    return asdict(state)


def n_navigation(state: DialogueState) -> dict:
    trace_node("navigation_node", state)
    navigation_handler(state, (state.last_outcome or {}).get("slots", {}))
    state.next_node = "respond"
    return asdict(state)


def n_cutlery(state: DialogueState) -> dict:
    trace_node("cutlery_node", state)
    cutlery_handler(state, (state.last_outcome or {}).get("slots", {}))
    state.next_node = "respond"
    return asdict(state)


def n_about(state: DialogueState) -> dict:
    trace_node("about_node", state)
    about_handler(state, (state.last_outcome or {}).get("slots", {}))
    state.next_node = "respond"
    return asdict(state)


def n_get_bill(state: DialogueState) -> dict:
    trace_node("get_bill_node", state)
    get_bill_handler(state)
    state.next_node = "respond"
    return asdict(state)


def n_normal_conv(state: DialogueState) -> dict:
    trace_node("normal_conv_node", state)
    normal_conv_handler(state, state.last_outcome)
    state.next_node = "respond"
    return asdict(state)


# --- take_order sub-flow ----------------------------------------------------

def n_new_order_start(state: DialogueState) -> dict:
    trace_node("new_order_start", state)
    nlu = state.last_outcome or {}
    slots = nlu.get("slots", {})
    order_action = slots.get("order_action")
    if order_action not in ("new_order", "edit_order", "cancel_order", "add_item"):
        order_action = "add_item" if state.current_order else "new_order"
    trace("take_order routing", order_action=order_action, current_order_nonempty=bool(state.current_order))
    state.active_intent = "take_order"
    state.order_action = order_action

    # if the item was already named in this same utterance (e.g. "I'd like
    # to order a veg burger"), don't throw it away and re-ask for it
    seed = nlu if order_action in ("new_order", "add_item") and slots.get("item_name") else None
    state.pending_seed = seed

    if order_action == "edit_order":
        state.next_node = "edit_order_start"
    elif order_action == "cancel_order":
        state.next_node = "cancel_order_start"
    else:
        if order_action == "add_item" and seed is None:
            speak("What would you like to add?")
        state.order_slot = "ask_item"
        state.next_node = "ask_item"
    return asdict(state)


def n_ask_item(state: DialogueState) -> dict:
    trace_node("ask_item", state)
    state.order_slot = "ask_item"
    outcome = get_outcome_or_route(state, "What item would you like?", "ask_item: which dish", "ask_item")
    if outcome is None:
        return asdict(state)

    # the user may have named more than one dish in the same breath ("a
    # paneer tikka and a crispy corn") — handle the first now, queue the
    # rest so they're picked up automatically afterward
    item_names = split_item_names(outcome["slots"].get("item_name"))
    item_name = item_names[0] if item_names else None
    match = find_menu_item(item_name) if item_name else None
    if not match:
        state.item_attempts += 1
        if state.item_attempts >= 3:
            speak("I'm having trouble catching that. Let's try again later.")
            state.went_idle = True
            state.next_node = "respond"
            return asdict(state)
        speak(f"Sorry, I couldn't find \"{item_name or 'that'}\" on the menu. "
              f"Could you say the dish name again?")
        state.next_node = "ask_item"
        return asdict(state)
    state.item_attempts = 0
    if len(item_names) > 1:
        state.pending_items.extend(item_names[1:])

    if match["category"] in QUICK_SERVE_CATEGORIES:
        # drinks/water etc. — no one interrogates a guest over
        # quantity/spice-level for a bottle of water; just bring it,
        # respecting a quantity if they already gave one
        qty_raw = outcome["slots"].get("quantity")
        try:
            qty = int(qty_raw)
        except (TypeError, ValueError):
            qty = None
        qty = qty if qty and qty > 0 else 1
        state.current_order.append({
            "item_id": match["item_id"], "name": match["name"], "category": match["category"],
            "quantity": qty, "preference": None, "confirmed": False,
        })
        trace("quick-serve item COMMITTED to current_order", **{"order": order_snapshot(state)})
        speak(f"Sure thing, bringing {qty}x {match['name']}.")
        if state.pending_items:
            state.pending_seed = seeded_outcome(item_name=state.pending_items.pop(0))
            state.next_node = "ask_item"
        else:
            state.next_node = "ask_to_add"
        return asdict(state)

    state.active_item = {
        "item_id": match["item_id"], "name": match["name"], "category": match["category"],
        "quantity": 1, "preference": None, "confirmed": False,
    }
    # if quantity was already given in the same breath ("two veg burgers"),
    # don't ask for it again
    qty_raw = outcome["slots"].get("quantity")
    try:
        qty = int(qty_raw)
    except (TypeError, ValueError):
        qty = None
    if qty and qty > 0:
        state.active_item["quantity"] = qty
        state.next_node = "ask_preference"
    else:
        state.next_node = "ask_qty"
    return asdict(state)


def n_ask_qty(state: DialogueState) -> dict:
    trace_node("ask_qty", state)
    state.order_slot = "ask_qty"
    prompt = f"How many {state.active_item['name']} would you like?"
    outcome = get_outcome_or_route(state, prompt, "ask_qty: how many", "ask_qty")
    if outcome is None:
        return asdict(state)

    qty_raw = outcome["slots"].get("quantity")
    try:
        qty = int(qty_raw)
    except (TypeError, ValueError):
        qty = None
    if not qty or qty <= 0:
        speak("Sorry, how many would you like? Please give me a number.")
        state.next_node = "ask_qty"
        return asdict(state)
    state.active_item["quantity"] = qty
    state.next_node = "ask_preference"
    return asdict(state)


def n_ask_preference(state: DialogueState) -> dict:
    trace_node("ask_preference", state)
    state.order_slot = "ask_preference"
    outcome = get_outcome_or_route(state, "Any preference, like spice level or no onions?",
                                    "ask_preference: preference for the current item", "ask_preference")
    if outcome is None:
        return asdict(state)

    pref = outcome["slots"].get("preference")
    if outcome.get("yes_no") == "no" or (pref and str(pref).strip().lower() in
                                          ("none", "no", "no preference", "nothing", "nope", "not really")):
        pref = None
    state.active_item["preference"] = pref
    state.current_order.append(state.active_item)
    trace("item COMMITTED to current_order", **{"order": order_snapshot(state)})
    state.active_item = None
    if state.pending_items:
        # another item was named in the same earlier breath — go straight
        # to it instead of asking "anything else?" first
        state.pending_seed = seeded_outcome(item_name=state.pending_items.pop(0))
        state.next_node = "ask_item"
    else:
        state.next_node = "ask_to_add"
    return asdict(state)


def n_ask_to_add(state: DialogueState) -> dict:
    trace_node("ask_to_add", state)
    state.order_slot = "ask_to_add"
    answer = get_yes_no_or_route(state, "Would you like to add anything else?", "ask_to_add")
    if answer is None:
        return asdict(state)
    if answer:
        add_nlu = state.last_outcome
        if add_nlu and add_nlu.get("slots", {}).get("item_name"):
            state.pending_seed = add_nlu
        state.next_node = "ask_item"
    else:
        state.next_node = "confirm_order"
    return asdict(state)


def n_confirm_order(state: DialogueState) -> dict:
    trace_node("confirm_order", state)
    state.order_slot = "confirm_order"
    answer = get_yes_no_or_route(state, "Shall I confirm the order?", "confirm_order")
    if answer is None:
        return asdict(state)
    if answer:
        place_order(state)
    else:
        speak("No problem — just let me know whenever you're ready to confirm.")
    state.next_node = "respond"
    return asdict(state)


# --- edit_order sub-flow ----------------------------------------------------

def n_edit_order_start(state: DialogueState) -> dict:
    trace_node("edit_order_start", state)
    state.order_slot = "edit_order_start"
    if not state.current_order:
        speak("You don't have any items in your order yet.")
        state.next_node = "respond"
        return asdict(state)

    outcome = get_outcome_or_route(state, "Which item do you want to edit?",
                                    "edit_order: which item", "edit_order_start")
    if outcome is None:
        return asdict(state)

    item_name = outcome["slots"].get("item_name")
    target = find_order_line(state, item_name)
    if not target:
        speak(f"I couldn't find \"{item_name or 'that item'}\" in your order.")
        state.next_node = "respond"
        return asdict(state)
    state.edit_target_name = target["name"]

    edit_action = outcome["slots"].get("edit_action") or "replace"
    if edit_action == "cancel_single":
        state.next_node = "edit_cancel_single"
    elif edit_action == "cancel_full":
        state.next_node = "edit_cancel_full"
    elif edit_action == "customize":
        state.next_node = "edit_customize"
    else:
        state.next_node = "edit_replace"
    return asdict(state)


def n_edit_cancel_single(state: DialogueState) -> dict:
    trace_node("edit_cancel_single", state)
    target = find_order_line(state, state.edit_target_name)
    if target:
        state.current_order.remove(target)
        speak(f"{target['name']} was removed from your order.")
    state.next_node = "respond"
    return asdict(state)


def n_edit_cancel_full(state: DialogueState) -> dict:
    trace_node("edit_cancel_full", state)
    state.current_order = []
    speak("The whole order was cancelled.")
    state.next_node = "respond"
    return asdict(state)


def n_edit_customize(state: DialogueState) -> dict:
    trace_node("edit_customize", state)
    state.order_slot = "edit_customize"
    target = find_order_line(state, state.edit_target_name)
    if not target:
        state.next_node = "respond"
        return asdict(state)
    outcome = get_outcome_or_route(state, f"What preference should I set for {target['name']}?",
                                    "edit_order: new preference", "edit_customize")
    if outcome is None:
        return asdict(state)
    target["preference"] = outcome["slots"].get("preference")
    speak(f"{target['name']} was customized.")
    state.next_node = "respond"
    return asdict(state)


def n_edit_replace(state: DialogueState) -> dict:
    trace_node("edit_replace", state)
    state.order_slot = "edit_replace"
    target = find_order_line(state, state.edit_target_name)
    if not target:
        state.next_node = "respond"
        return asdict(state)
    outcome = get_outcome_or_route(state, "What would you like instead?",
                                    "edit_order: replacement item", "edit_replace")
    if outcome is None:
        return asdict(state)
    new_name = outcome["slots"].get("item_name")
    match = find_menu_item(new_name)
    if not match:
        speak(f"Sorry, I couldn't find \"{new_name or 'that'}\" on the menu.")
    else:
        target["item_id"] = match["item_id"]
        target["name"] = match["name"]
        target["category"] = match["category"]
        speak(f"Replaced with {match['name']}.")
    state.next_node = "respond"
    return asdict(state)


# --- cancel_order (delete a single item) sub-flow ---------------------------

def n_cancel_order_start(state: DialogueState) -> dict:
    trace_node("cancel_order_start", state)
    state.order_slot = "cancel_order_start"
    if not state.current_order:
        speak("You don't have any items in your order yet.")
        state.next_node = "respond"
        return asdict(state)

    outcome = get_outcome_or_route(state, "Which item should I delete?",
                                    "cancel_order: which item", "cancel_order_start")
    if outcome is None:
        return asdict(state)

    item_name = outcome["slots"].get("item_name")
    target = find_order_line(state, item_name)
    if not target:
        speak(f"I couldn't find \"{item_name or 'that item'}\" in your order.")
    else:
        state.current_order.remove(target)
        speak(f"Got it, removing {target['name']} from your order.")
    state.next_node = "respond"
    return asdict(state)


# ---------------------------------------------------------------------------
# Graph assembly
# ---------------------------------------------------------------------------

NODE_FUNCS = {
    "intent_classify": n_intent_classify,
    "handle_switch": n_handle_switch,
    "respond": n_respond,
    "menu_node": n_menu,
    "call_people_node": n_call_people,
    "navigation_node": n_navigation,
    "cutlery_node": n_cutlery,
    "about_node": n_about,
    "get_bill_node": n_get_bill,
    "normal_conv_node": n_normal_conv,
    "new_order_start": n_new_order_start,
    "ask_item": n_ask_item,
    "ask_qty": n_ask_qty,
    "ask_preference": n_ask_preference,
    "ask_to_add": n_ask_to_add,
    "confirm_order": n_confirm_order,
    "edit_order_start": n_edit_order_start,
    "edit_replace": n_edit_replace,
    "edit_customize": n_edit_customize,
    "edit_cancel_single": n_edit_cancel_single,
    "edit_cancel_full": n_edit_cancel_full,
    "cancel_order_start": n_cancel_order_start,
}


def _route_by_next_node(state: DialogueState) -> str:
    return state.next_node


def build_graph():
    g = StateGraph(DialogueState)
    for name, fn in NODE_FUNCS.items():
        g.add_node(name, fn)
    g.set_entry_point("intent_classify")

    edge_map = {name: name for name in NODE_FUNCS}
    edge_map["END"] = END
    for name in NODE_FUNCS:
        g.add_conditional_edges(name, _route_by_next_node, edge_map)

    return g.compile(checkpointer=MemorySaver())


# ---------------------------------------------------------------------------
# Outer driver: runs the compiled graph, handling its interrupt/resume
# protocol with real (or text-mode) speak/listen I/O.
# ---------------------------------------------------------------------------

def run_graph_session(app, carried_state: Optional[DialogueState], thread_id: str) -> DialogueState:
    """Drives one compiled LangGraph run start-to-finish. Whenever a node
    calls interrupt() (asking the guest something), we speak the prompt,
    listen for a reply (with the usual reprompt-once-on-silence), and resume
    the graph with either the transcript or a timeout sentinel. One call
    here = one wake-word activation; current_order/table_no/bill_total
    persist across calls via carried_state (see main_loop)."""
    global _current_state

    state = carried_state if carried_state is not None else DialogueState()
    if FORCED_TABLE_NO and not state.table_no:
        # Staff-dispatched session (backend/launcher.py set TABLE_NO for this
        # process) — skip conversational discovery, same effect as a guest
        # having already stated their table. db_start_session() below
        # resolves the visit against this immediately.
        state.table_no = FORCED_TABLE_NO
        state.table_no_locked = True
    # Per-wake-word-activation fields reset; per-visit fields (current_order,
    # table_no, bill_total, db_visit_id, db_service_point_id...) are left as
    # carried forward from the previous session.
    state.db_session_id = None
    state.pending_question = None
    state.next_node = None
    state.went_idle = False
    state.item_attempts = 0
    state.pending_seed = None
    state.first_turn = True

    _current_state = state
    db_start_session(state)

    config = {"configurable": {"thread_id": thread_id}}
    result = app.invoke(state, config)
    while True:
        interrupts = result.get("__interrupt__")
        if not interrupts:
            result.pop("__interrupt__", None)
            valid_fields = DialogueState.__dataclass_fields__
            return DialogueState(**{k: v for k, v in result.items() if k in valid_fields})
        payload = interrupts[0].value
        prompt_text = payload["prompt"] if isinstance(payload, dict) else str(payload)
        transcript = listen_with_reprompt(prompt_text)
        resume_value = transcript if transcript is not None else TIMEOUT_SENTINEL
        result = app.invoke(Command(resume=resume_value), config)


# ---------------------------------------------------------------------------
# Staff-dispatched delivery routine — distinct from run_graph_session()'s
# guest conversations. Triggered by the dashboard's "Deliver Order" button
# (SONIC_ACTION_HINT=deliver), not a guest talking to the robot.
# ---------------------------------------------------------------------------

PICKUP_CONFIRM_WINDOW_S = 30.0
# Deliberately a small local phrase set, not a full NLU round-trip — this is
# a narrow yes/no from kitchen staff ("done"/"ready"/"loaded"/"go"), not a
# guest-ordering utterance, so run_nlu()'s take-order-oriented prompt isn't
# a good fit. Mirrors the existing is_farewell_text()/FAREWELL_PHRASES
# pattern already used elsewhere in this file for the same reason.
PICKUP_AFFIRMATIVE_PHRASES = (
    "yes", "yeah", "yep", "yup", "done", "ready", "go", "go ahead",
    "loaded", "it's loaded", "its loaded", "all set", "all done", "confirmed",
)


def _sounds_like_pickup_confirmed(text: str) -> bool:
    t = text.strip().lower()
    return any(t == p or p in t for p in PICKUP_AFFIRMATIVE_PHRASES)


def run_delivery_session(table_no: str) -> None:
    """The robot is already at the kitchen (main_loop's caller navigates
    there first): ask staff to load the order, wait up to
    PICKUP_CONFIRM_WINDOW_S for a spoken confirmation, and only THEN travel
    to the table — never delivering an empty robot. A missed/negative
    confirmation aborts the trip rather than guessing."""
    speak(f"Order for Table {table_no} is ready to load. Please place it on me and say ready when done.")
    reply = listen(timeout_s=PICKUP_CONFIRM_WINDOW_S)
    if not reply or not _sounds_like_pickup_confirmed(reply):
        speak("I didn't hear a confirmation, so I'll stay here — please send me again once it's loaded.")
        trace("delivery aborted: no pickup confirmation", table_no=table_no, heard=reply)
        return

    arrived = perform_travel_action(
        "Order confirmed, heading to the table now.",
        f"Table {table_no}",
        arrival_text=f"Here's your order for Table {table_no}. Enjoy your meal!",
    )
    if not arrived:
        trace("delivery failed: could not reach table", table_no=table_no)


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def main_loop() -> None:
    app = build_graph()
    thread_id = f"sonic-{uuid_module.uuid4().hex[:8]}"
    carried_state: Optional[DialogueState] = None

    if TEXT_MODE:
        print("=== Sonic (text mode) — no mic, no wake word, no Sarvam calls ===")
        print("Type your first utterance directly (the greeting is simulated).")
        print("Press Enter on a blank line to simulate silence/timeout. Type 'quit' to exit.\n")
        while True:
            carried_state = run_graph_session(app, carried_state, thread_id)
    elif FORCED_TABLE_NO:
        # Staff-dispatched session (backend/launcher.py spawned this process
        # scoped to one table via TABLE_NO/SONIC_ACTION_HINT) — skip the
        # wake-word gate and run exactly one round trip, then exit, mirroring
        # the old test_harness.py's one-shot-subprocess-per-click lifecycle.
        # The robot's home base is the kitchen: every dispatched task is
        # Kitchen -> (task) -> Kitchen. A failed outbound leg skips the task
        # rather than pretending to be somewhere it isn't; the return leg is
        # best-effort and never blocks the process from exiting.
        require_api_keys()
        action_label = FORCED_ACTION or "order"
        print(f"Sonic dispatched: table={FORCED_TABLE_NO} action={action_label!r} (no wake word needed).")
        navigate_and_wait("Kitchen")  # known start point; idempotent if already there
        if FORCED_ACTION == "deliver":
            run_delivery_session(FORCED_TABLE_NO)
        else:
            # No arrival_text here on purpose: run_graph_session()'s entry
            # node (n_intent_classify) already greets and asks "Hi! How can
            # I help you today?" via its own interrupt()/listen() as soon as
            # it starts — an extra greeting here would double up, delaying
            # the guest's chance to answer within n_intent_classify's own
            # listen window (a real bug this caused: robot arrives, speaks
            # two greetings back to back, times out waiting for a reply,
            # and leaves without ever taking the order).
            arrived = perform_travel_action(
                f"Heading to Table {FORCED_TABLE_NO} now, I'll be right there!",
                f"Table {FORCED_TABLE_NO}",
            )
            if arrived:
                run_graph_session(app, carried_state, thread_id)
        navigate_and_wait("Kitchen")
    else:
        require_api_keys()
        oww_model = load_wake_word_model()
        print(f"Sonic is listening for the wake word ('{WAKE_WORD_NAME}')...")
        while True:
            wait_for_wake_word(oww_model)
            carried_state = run_graph_session(app, carried_state, thread_id)


def main() -> None:
    global TEXT_MODE
    parser = argparse.ArgumentParser(description="Sonic restaurant robot voice agent")
    parser.add_argument("--text-mode", action="store_true",
                        help="Run with typed input/printed output instead of mic/speaker/wake-word")
    args = parser.parse_args()
    TEXT_MODE = args.text_mode

    if TEXT_MODE:
        require_api_keys(need_sarvam=False)
    print("Runtime: LangGraph StateGraph (graph.py's node/edge design, now actually executed via "
          "interrupt()/Command(resume=...) + a MemorySaver checkpointer).")
    print(f"Node-execution tracing: {'ON' if TRACE_ENABLED else 'OFF'} (set SONIC_TRACE=0 to disable)")
    resolve_robot()
    load_menu_from_db()
    if FORCED_TABLE_NO:
        print(f"[db] TABLE_NO={FORCED_TABLE_NO!r} set — session(s) will skip conversational table discovery")
    main_loop()


if __name__ == "__main__":
    main()
