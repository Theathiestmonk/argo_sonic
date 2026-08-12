#!/usr/bin/env python3
"""
backend/launcher.py
────────────────────
Tiny standalone HTTP server that lets the browser start / stop the
full Argo robot stack without SSH.

This file lives in   backend/   (NOT inside the ROS package) because it has
zero ROS dependencies — it just launches a shell script in a subprocess.

Run once on the robot before opening the UI:
    python3 ~/dhruvil/argo_sonic/backend/launcher.py

Endpoints (CORS-open so the browser can call them directly):
    GET  /status  →  {"running": bool, "pid": int|null, "mode": str|null, "map": str|null}
    GET  /config  →  {"maps_dir": str}  (absolute path to src/argo_mini/maps
                      on THIS checkout — the frontend uses this instead of a
                      hardcoded guess, since where the repo is cloned can vary)
    GET  /maps    →  {"maps": [str, ...]}  (map names, from *.yaml in MAPS_DIR)
    GET  /ntfields_models →  {"models": [str, ...]}  (map names with a trained
                      <name>.pt in NTFIELDS_MODELS_DIR — SettingsPanel.jsx's
                      "NTFields model ready"/"No NTFields model" badge)
    GET  /ntfields/train/status → {"running": bool, "map": str|null,
                      "phase": "training"|"done"|"failed"|null, "phase_text": str|null}
    POST /ntfields/train  →  body {"map": str}. Starts offline NTFields
                      training (src/argo_mini/argo_mini/ntfields_offline_train.py)
                      for that map in a background thread (_ntfields_train_worker),
                      writing NTFIELDS_MODELS_DIR/<map>.pt — argo_sonic_nav.py's
                      "navigate" mode needs that file to exist for the map it's
                      given. Triggered automatically by the setup wizard right
                      after a map is saved (ExplorationPanel.jsx's saveMap()).
                      404 if that map hasn't been saved yet; 409
                      (ntfields_train_busy) if a training job is already
                      running — no exclusion against /voice/start or
                      /nav/goto, training never touches Nav2/serial/the mic.
    GET  /maps/<name>/meta         →  {"resolution": float, "origin": [x,y,theta]}
                                       parsed from <name>.yaml — lets the frontend
                                       place waypoints on the map image correctly
    GET  /maps/<name>/preview      →  raw bytes of <name>.pgm
    GET  /waypoints/<map_name>     →  JSON content of waypoints/<map_name>.json
                                       (empty {} if that map has no waypoints yet)
    POST /waypoints/<map_name>     →  body is the full waypoints dict; overwrites
                                       the file (mirrors waypoint_manager.py's own
                                       full-rewrite save_waypoints() semantics)
    GET  /menu    →  {"menu": [...], "settings": {...}, "savedAt": str} sourced
                      from Postgres (menu_items/menu_categories/menu_settings —
                      see sonic/*.sql, sonic/seed_db.py), one global menu, not
                      per-map (a venue has one menu regardless of which SLAM
                      map is loaded). Same wire shape as the old file-based
                      version, so frontend/public/menu-data.js and menu.html
                      need no changes.
    POST /menu    →  body is {"menu": [...], "settings": {...}, "savedAt": str};
                      full-overwrite upsert into Postgres (deletions included —
                      an item missing from the body is removed). menu-data.js
                      POSTs here after every localStorage save in menu.html.
                      sonic/main_agent.py reads the same tables directly at
                      startup (load_menu_from_db()), not through this endpoint.
    GET  /orders/<map_name>            →  {table_id: {items, total, status,
                                           updatedAt}, ...} for every table with
                                           an active visit, from Postgres
                                           (orders/order_items/visits/
                                           service_points). map_name is accepted
                                           for URL compatibility but not filtered
                                           on — Postgres has one table set per
                                           location, not per map.
    GET  /orders/<map_name>/<table_id> →  just that table's order ({} if none)
    POST /orders/<map_name>/<table_id> →  501 — orders are now written directly
                                           by sonic/main_agent.py's
                                           db_place_order() as the guest orders,
                                           not through this endpoint.
    DELETE /orders/<map_name>/<table_id> → closes that table's active visit
                                           (Postgres equivalent of clearing a
                                           table's card); no-op (still 200) if
                                           there wasn't one
    POST /start   →  body {"mode": "manual"|"auto"|"navigate", "map": str} (default "auto")
                      "manual"   → sh/start_slam_ui.sh          (SLAM only, no Nav2 — build a map)
                      "auto"     → sh/start_slam_explore_ui.sh  (SLAM + Nav2 + frontier explorer)
                      "navigate" → python3 argo_sonic_nav.py --map <MAPS_DIR>/<map> --no-rviz
                                    (SLAM-toolbox localization + Nav2 with the
                                    NTFields physics-informed planner in place of
                                    nav2_planner, on a previously saved map —
                                    "map" is required; needs a pretrained
                                    ~/ntfields_models/<map>.pt for that map)
                      If something's already running under a different mode/map than
                      requested, it's stopped first so the new one always reflects what
                      was actually asked for (e.g. switching which map to navigate on).
                      For "navigate" specifically: a background thread
                      (_watch_for_nav_ready) polls NAV_PROGRESS_PATH for THIS run
                      reporting READY, then auto-starts main_agent.py's continuous
                      wake-word loop ("Hi Sonic") — so a guest can talk to the robot
                      without staff separately starting it by hand. See GET
                      /voice/status's wake_loop_* fields.
    POST /stop    →  kills the entire process group cleanly, and stops the
                      wake-word loop too if it had been auto-started (it only
                      makes sense while Nav2 is up)
    GET  /nav_progress → {"status": "OK"|"ERROR"|"READY"|"STOPPED"|null,
                          "message": str|null, "timestamp": int|null} —
                          the current/last step NAV_SCRIPT reported
                          (argo_sonic_nav.py, and the two now-unused
                          sh/start_argo_nav_ui.sh / start_ntfields_nav_ui.sh,
                          all write to the same file in this same format —
                          only one "navigate" mode process runs at a time),
                          read from NAV_PROGRESS_PATH. "status": null means the file
                          doesn't exist yet (nothing has ever reported in).
    GET  /nav_log → {"log": str} — the last NAV_LOG_TAIL_LINES lines of
                     NAV_SCRIPT's actual stdout+stderr (every ROS node's own
                     INFO/WARN/ERROR output, not just the high-level steps
                     /nav_progress tracks), read from NAV_LOG_PATH. A plain
                     file (not subprocess.PIPE) is still safe for this
                     single-threaded server — a file never needs the parent
                     to actively drain it, so a chatty/slow node still can't
                     wedge anything here — and means a real crash traceback
                     shows up over HTTP instead of requiring SSH + running
                     the script by hand to ever see it.

    GET  /voice/status →  {"running": bool, "pid": int|null, "action": str|null,
                           "map": str|null, "table": str|null,
                           "wake_loop_running": bool, "wake_loop_pending": bool,
                           "phase": str|null, "phase_text": str|null} —
                           is Sonic currently mid-conversation at a table
                           ("running"/action/map/table), and separately, is the
                           continuous wake-word loop up ("wake_loop_running") or
                           does it want to be but isn't right now
                           ("wake_loop_pending" — paused for a table dispatch, or
                           still starting up after nav just went READY)? "phase"/
                           "phase_text" (e.g. "heading_to_table"/"Heading to
                           Table 3") come from main_agent.py's own report_phase()
                           via VOICE_PROGRESS_PATH — null once stale (see
                           VOICE_PROGRESS_MAX_AGE_S) or if nothing's in progress.
    POST /voice/start  →  body {"action": "order"|"deliver"|"bill"|"room_service",
                           "map": str, "table": str}. Spawns sonic/main_agent.py
                           as a subprocess scoped to that table — this is what a
                           table's action buttons in the UI actually call. Pauses
                           the wake-word loop for the dispatch's duration (shared
                           mic/speaker, can't run both at once) and restarts it
                           once the dispatch process exits, if it's still supposed
                           to be running.
                           main_agent.py is normally a continuous wake-word
                           loop that discovers its table conversationally; a
                           TABLE_NO env var (set here from "table") makes it
                           skip that and run exactly one Kitchen->Table N->
                           Kitchen round trip for this table instead (real
                           Nav2 navigation, see sonic/nav_bridge.py), then
                           exit — same click-to-dispatch lifecycle the old
                           sonic/test_harness.py had. "action" is passed
                           through as SONIC_ACTION_HINT: order and room_service
                           both run the full humanized take-order conversation;
                           bill and deliver currently get a brief spoken apology
                           and a return to the kitchen (not yet rewired into
                           main_agent.py — see its module docstring) rather than
                           the dedicated bill/pickup-confirmation behavior the
                           older sonic_agent.py had. SONIC_MAP_NAME
                           (from "map") selects which
                           src/argo_mini/waypoints/<map>.json to navigate
                           against.
                           Same action+map+table while already running → no-op
                           "already_running". A DIFFERENT one while already
                           running → REJECTED (409) rather than pre-empted —
                           Sonic can only physically talk to one table at a
                           time, and silently killing an in-progress
                           conversation to start another would lose it.
    POST /voice/stop   →  kills the current voice-session process group,
                           independent of the SLAM/Nav2 stack above (separate
                           state, separate lock — starting/stopping a voice
                           session must never contend with or depend on
                           whether the nav stack is running).
    GET  /voice/nav_enabled  → {"enabled": bool} — staff-facing kill-switch
                           (locations.voice_nav_enabled) that main_agent.py's
                           navigate_and_wait() checks before every Kitchen<->
                           Table trip (dispatched or voice-triggered take_order
                           alike). Defaults true so a missing DB doesn't read
                           as "disabled". A guest's own free-form "take me to
                           X" request isn't a live intent yet (see n_stub) —
                           this switch already covers it once that's rewired
                           in, since it gates navigate_and_wait() itself.
    POST /voice/nav_enabled → body {"enabled": bool}; toggled from the
                           dashboard (see TablesPanel.jsx).

    GET  /nav/goto/status → {"running": bool, "destination": str|null,
                           "phase": "heading"|"arrived"|"failed"|null,
                           "phase_text": str|null} — status of the current/
                           last /nav/goto trip (see below).
    POST /nav/goto     →  body {"destination": str, "map": str}. A plain,
                           conversation-free single-destination trip — e.g.
                           the "Go to kitchen" button — run in a background
                           thread inside this process (_nav_goto_worker),
                           shelling out to sonic/nav_bridge.py exactly the
                           way main_agent.py's own navigate_and_wait() does,
                           gated by the same voice_nav_enabled() kill-switch.
                           Rejected (409) while a table dispatch is active
                           (voice_session_busy) or another /nav/goto trip is
                           already running (nav_goto_busy) — same reasoning
                           as /voice/start's own busy check, since a voice
                           dispatch and a plain goto trip can likewise never
                           run at once. This replaces the frontend's old
                           direct rosbridge goal-sending for such buttons —
                           the frontend now only ever triggers navigation
                           through this endpoint or POST /voice/start, never
                           by publishing a Nav2 goal itself.

    GET  /estop/status →  {"estopped": bool} — is serial_bridge currently
                           killed via /estop?
    POST /estop        →  emergency stop: immediately kills serial_bridge
                           (cuts motor commands/power) by process name,
                           independent of the rest of the nav stack (SLAM,
                           planner, etc. keep running) — for when the robot
                           is physically misbehaving and needs motors cut
                           right now, not a graceful stop of everything.
    POST /estop/resume →  relaunches serial_bridge directly to resume manual
                           control after an /estop, without restarting the
                           whole nav stack.

    GET  /battery →  {"connected": bool, "voltage": float, "current": float,
                      "battery_percent": float, "charging": bool,
                      "estimated_remaining_hours": float,
                      "estimated_remaining_seconds": int,
                      "estimated_charge_remaining_hours": float,
                      "estimated_charge_remaining_seconds": int,
                      "temperatures": [float, ...]} — live BMS reading over
                      Bluetooth (same JBD-protocol pack src/argo_mini/argo_mini/
                      dashboard.py originally read), polled in a background
                      thread started at the bottom of this file. "connected":
                      false means the BMS hasn't been reached yet (still
                      scanning, or the pack is off/out of range) — every
                      other field is 0/empty in that case, not stale data.
                      "charging" is derived from the pack's own current sign
                      (positive = charging); estimated_remaining_* is only
                      meaningful while discharging, estimated_charge_
                      remaining_* only while charging — the frontend picks
                      whichever applies via "charging".

These *_ui.sh scripts are dedicated copies of the hand-run sh/start_slam.sh
and sh/start_slam_explore.sh — kept separate so UI-driven launches never
disturb the scripts used directly over SSH.
"""

import asyncio
import datetime
import json
import os
import re
import shlex
import signal
import subprocess
import threading
import time
import uuid as uuid_lib
from http.server import BaseHTTPRequestHandler, HTTPServer

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

# ── Config ──────────────────────────────────────────────────────────────────

# Root of this repo (backend/ is one level below root)
_HERE   = os.path.dirname(os.path.abspath(__file__))
_ROOT   = os.path.dirname(_HERE)

SLAM_SCRIPT    = os.path.join(_ROOT, 'sh', 'start_slam_ui.sh')
EXPLORE_SCRIPT = os.path.join(_ROOT, 'sh', 'start_slam_explore_ui.sh')
# NTFields nav stack, at the repo root (not sh/) — this is a Python launcher,
# not a bash script, so it's invoked with the interpreter, not `bash`, in
# the "navigate" branch below. Uses ntfields_planner_node, which is this
# repo's own NTFields implementation (confirmed by checking which
# executables are actually built where — see this file's own POST /start
# NAV_SCRIPT history/comments and argo_sonic_nav.py's header for the story
# of why start_argo_nav_ui.sh / start_ntfields_nav_ui.sh aren't used here).
NAV_SCRIPT     = os.path.join(_ROOT, 'argo_sonic_nav.py')
MAPS_DIR       = os.path.join(_ROOT, 'src', 'argo_mini', 'maps')
WAYPOINTS_DIR  = os.path.join(_ROOT, 'src', 'argo_mini', 'waypoints')
SONIC_DIR      = os.path.join(_ROOT, 'sonic')
SONIC_SCRIPT   = os.path.join(SONIC_DIR, 'main_agent.py')
VOICE_LOG_PATH = os.path.join(SONIC_DIR, 'voice_session.log')
# Same ROS-aware one-shot bridge main_agent.py's navigate_and_wait() shells
# out to — used directly here (background thread, not a subprocess of
# main_agent.py) for single-destination trips that have no conversation
# attached (POST /nav/goto, e.g. the "Go to kitchen" button).
NAV_BRIDGE_SCRIPT = os.path.join(SONIC_DIR, 'nav_bridge.py')
# Offline NTFields training (POST /ntfields/train) — pure Python/torch, no
# rclpy import at all (confirmed by reading the script), so unlike
# NAV_BRIDGE_SCRIPT this needs no ROS environment sourced, just a plain
# `python3` subprocess. NTFIELDS_MODELS_DIR matches the exact convention
# antfields-demo/train_ntfields.py and argo_sonic_nav.py's own "navigate"
# mode already use — a model is looked up by map name at <dir>/<map>.pt.
NTFIELDS_TRAIN_SCRIPT = os.path.join(_ROOT, 'src', 'argo_mini', 'argo_mini', 'ntfields_offline_train.py')
NTFIELDS_MODELS_DIR = os.path.expanduser('~/ntfields_models')

# Menu + orders now live in Postgres (see sonic/*.sql, sonic/seed_db.py)
# instead of src/argo_mini/menu/menu.json + src/argo_mini/orders/*.json —
# load the same .env main_agent.py uses so DATABASE_URL/ROBOT_UID agree.
load_dotenv(os.path.join(SONIC_DIR, '.env'))
DATABASE_URL = os.environ.get('DATABASE_URL')
ROBOT_UID    = os.environ.get('ROBOT_UID', 'SONIC-001')
# Frontend-generated ids (e.g. "m1784625561831-1", for a not-yet-saved menu
# item) get mapped deterministically to a menu_items.menu_item_id UUID so
# repeated saves before a page refresh don't create duplicate rows — same
# namespace/approach as sonic/seed_db.py's menu_item_uuid().
MENU_UUID_NAMESPACE = uuid_lib.UUID('6f6a1e2e-4b8a-5e3a-9c2a-2f2f2f2f2f2f')
_VOICE_ACTIONS = {'order', 'deliver', 'bill', 'room_service'}
NAV_PROGRESS_PATH = '/tmp/argo_nav_progress'  # written by NAV_SCRIPT (whichever nav script is active)
NAV_LOG_PATH       = '/tmp/argo_nav_output.log'  # full stdout+stderr of the current/last NAV_SCRIPT run
NAV_LOG_TAIL_LINES = 300
VOICE_PROGRESS_PATH = '/tmp/argo_voice_progress'  # written by main_agent.py's report_phase() — read by /voice/status
VOICE_PROGRESS_MAX_AGE_S = 30  # ignore (and don't surface) a phase report older than this — self-heals a stale file
PORT    = 8888


# ── Battery (BMS over Bluetooth) ─────────────────────────────────────────────
# Ported from src/argo_mini/argo_mini/dashboard.py, which already had this
# working (JBD-protocol BLE pack, GET /api/bms) — moved here instead of
# calling out to that separate process, since this file is the one backend
# the frontend actually talks to for everything else. Only the BMS
# read/estimate logic is ported, not dashboard.py's own Bluetooth-cache-reset
# step in main() (a `sudo rm -rf /var/lib/bluetooth/*` on every launch) —
# that's a separate, riskier concern this file shouldn't take on silently.
BMS_ADDRESS = "A5:C2:37:2A:22:EC"
BMS_RX_CHAR = "0000ff01-0000-1000-8000-00805f9b34fb"
BMS_TX_CHAR = "0000ff02-0000-1000-8000-00805f9b34fb"

# Calibrated runtime: measured hours for a full pack under normal load.
# ETA is linear against this, same model dashboard.py used.
CALIBRATED_FULL_RUNTIME_HOURS = 7.0

_latest_bms_data = {
    "voltage": 0.0,
    "current": 0.0,
    "remaining_capacity": 0.0,
    "full_capacity": 0.0,
    "battery_percent": 0.0,
    "charging": False,
    "estimated_remaining_hours": 0.0,
    "estimated_remaining_seconds": 0,
    "estimated_charge_remaining_hours": 0.0,
    "estimated_charge_remaining_seconds": 0,
    "temperatures": [],
    "connected": False,
}

# JBD's basic-info current field is signed: positive = charging (current
# flowing into the pack), negative = discharging. A small deadband around
# zero avoids "charging" flapping on/off from noise while the pack sits
# essentially idle. Flip the sign here if this reads backwards on your
# actual wiring — the JBD spec is consistent about it, but pack-to-pack
# integrations have been seen wired with current reversed.
_CHARGE_CURRENT_DEADBAND_A = 0.05


def _estimate_remaining_hours(percent: float) -> float:
    if percent <= 0:
        return 0.0
    return CALIBRATED_FULL_RUNTIME_HOURS * (percent / 100.0)


def _estimate_charge_remaining_hours(current_a: float, remaining_ah: float, full_ah: float) -> float:
    """Hours until full, from the pack's own reported capacity gap and its
    actual live charge current — not the calibrated-runtime model above,
    since charge current (charger-limited) has nothing to do with discharge
    load. 0.0 if not actually charging or full_ah is unknown."""
    if current_a <= _CHARGE_CURRENT_DEADBAND_A or full_ah <= 0:
        return 0.0
    capacity_needed_ah = max(0.0, full_ah - remaining_ah)
    return capacity_needed_ah / current_a


def _bms_notification_handler(_sender, data, _packet_buf: bytearray):
    _packet_buf.extend(data)


def _try_power_on_bluetooth():
    """Best-effort: clear any rfkill soft-block, then power the adapter on
    via bluetoothctl — the actual fix for bleak's "No powered Bluetooth
    adapters found" error. Deliberately NOT dashboard.py's approach
    (`rm -rf /var/lib/bluetooth/*` + `systemctl restart bluetooth`): that
    wipes every paired Bluetooth device on the machine, needs sudo, and
    only re-powers the radio at all if AutoEnable=true happens to be set in
    /etc/bluetooth/main.conf. Neither step here needs sudo on a normal
    desktop polkit setup (confirmed: `rfkill unblock bluetooth` runs as a
    plain user; `bluetoothctl power on` then succeeds once the soft-block
    dropped from a laptop's Fn-key/airplane-mode toggle is cleared — that
    combination, not a powered-off radio, was the actual cause here).
    Silently no-ops if either tool is missing or the calls fail — the BMS
    loop just keeps retrying either way."""
    try:
        subprocess.run(['rfkill', 'unblock', 'bluetooth'], capture_output=True, timeout=5, text=True)
    except Exception:
        pass
    try:
        subprocess.run(['bluetoothctl', 'power', 'on'], capture_output=True, timeout=10, text=True)
    except Exception:
        pass


def _bluetoothctl_info(address: str) -> str:
    """Raw `bluetoothctl info <address>` output (stdout+stderr), or '' on
    any failure. This is a local query against what BlueZ already knows —
    it does NOT require the device to be actively advertising right now,
    unlike a scan. Used to tell apart "BlueZ has never heard of this
    device" (needs a real scan) from "BlueZ already knows/holds it"
    (a scan would be pointless, or actively misleading)."""
    try:
        result = subprocess.run(['bluetoothctl', 'info', address], capture_output=True, timeout=8, text=True)
        return (result.stdout or '') + (result.stderr or '')
    except Exception:
        return ''


def _bluetoothctl_disconnect(address: str):
    try:
        subprocess.run(['bluetoothctl', 'disconnect', address], capture_output=True, timeout=8, text=True)
    except Exception:
        pass


async def _bms_poll_session(client, packet_buf: bytearray):
    """Poll loop for an already-connected client — runs until disconnected
    or an unrecoverable error. Shared by both connection paths below."""
    cmd = bytes.fromhex("DD A5 03 00 FF FD 77")
    while client.is_connected:
        packet_buf.clear()
        await client.write_gatt_char(BMS_TX_CHAR, cmd, response=False)
        await asyncio.sleep(2.5)  # wait for the notify stream

        data = bytes(packet_buf)
        if len(data) >= 30:
            try:
                voltage  = int.from_bytes(data[4:6], "big") / 100.0
                current  = int.from_bytes(data[6:8], "big", signed=True) / 100.0
                rem_cap  = int.from_bytes(data[8:10], "big") / 100.0
                full_cap = int.from_bytes(data[10:12], "big") / 100.0
                percent  = (rem_cap / full_cap * 100.0) if full_cap > 0 else 0.0

                temp_count = data[26]
                temps, offset = [], 27
                for _ in range(min(temp_count, 4)):
                    if offset + 2 <= len(data):
                        temps.append((int.from_bytes(data[offset:offset + 2], "big") - 2731) / 10.0)
                        offset += 2

                charging = current > _CHARGE_CURRENT_DEADBAND_A
                eta_hours = _estimate_remaining_hours(percent)
                charge_eta_hours = _estimate_charge_remaining_hours(current, rem_cap, full_cap)
                _latest_bms_data.update({
                    "voltage": voltage, "current": current,
                    "remaining_capacity": rem_cap, "full_capacity": full_cap,
                    "battery_percent": percent,
                    "charging": charging,
                    "estimated_remaining_hours": eta_hours,
                    "estimated_remaining_seconds": max(0, int(round(eta_hours * 3600))),
                    "estimated_charge_remaining_hours": charge_eta_hours,
                    "estimated_charge_remaining_seconds": max(0, int(round(charge_eta_hours * 3600))),
                    "temperatures": temps, "connected": True,
                })
            except Exception as parse_error:
                print(f'[launcher][bms] packet parse failed: {parse_error}')

        await asyncio.sleep(3.0)


async def _bms_telemetry_loop():
    """Runs forever in its own thread/event loop (see run_bms_thread below).
    Same JBD BLE protocol dashboard.py used, reconnecting on any failure
    rather than crashing this whole server.

    Connects by address FIRST, without scanning — if BlueZ already knows
    the device (paired, or seen earlier this boot/run), that succeeds
    immediately. dashboard.py's original version always scanned first on
    every single retry, which is what produced an endless "searching..."
    loop even once the pack was already reachable — a fresh discovery scan
    every ~15s instead of just reconnecting to a known device.

    A raw scan (BleakScanner.find_device_by_address) only ever finds a
    device that's currently ADVERTISING — most BLE peripherals (this BMS
    included) stop advertising once something already holds a connection to
    them. So if the pack is already connected elsewhere (a previous run of
    this same process that didn't exit cleanly, another tool, a phone app),
    a plain scan would just hang for its own timeout and find nothing, over
    and over, even though the device is right there. Before falling back to
    a real scan, check what BlueZ itself already knows via `bluetoothctl
    info` (a local query, doesn't need the device to be advertising) —
    if it's already connected, disconnect it so we can take over cleanly;
    if BlueZ already has it cached at all, a direct connect retry should
    just work without needing a scan."""
    from bleak import BleakClient, BleakScanner  # optional dep — see run_bms_thread's guard

    packet_buf = bytearray()

    def on_notify(sender, data):
        _bms_notification_handler(sender, data, packet_buf)

    async def try_direct_connect() -> bool:
        """One direct-by-address connect + full session. True if a session
        actually ran (i.e. it connected — whether it later disconnected
        normally or errored out mid-session doesn't matter, the caller's
        job either way is just to loop back and try again)."""
        try:
            async with BleakClient(BMS_ADDRESS, timeout=10.0) as client:
                print('[launcher][bms] connected (direct)')
                await client.start_notify(BMS_RX_CHAR, on_notify)
                await _bms_poll_session(client, packet_buf)
            return True
        except Exception as direct_err:
            print(f'[launcher][bms] direct connect failed ({direct_err})')
            return False

    loop = asyncio.get_event_loop()

    while True:
        try:
            if await try_direct_connect():
                continue  # ran a session — go straight back to another direct attempt

            info = await loop.run_in_executor(None, _bluetoothctl_info, BMS_ADDRESS)
            already_connected = 'Connected: yes' in info
            known_to_bluez = bool(info.strip()) and 'not available' not in info.lower()

            if already_connected:
                print('[launcher][bms] BlueZ already shows this device connected (elsewhere) — disconnecting it so we can take over...')
                await loop.run_in_executor(None, _bluetoothctl_disconnect, BMS_ADDRESS)
                await asyncio.sleep(1.0)
                if await try_direct_connect():
                    continue
            elif known_to_bluez:
                print('[launcher][bms] BlueZ already knows this device — retrying a direct connect instead of scanning...')
                if await try_direct_connect():
                    continue

            print(f'[launcher][bms] scanning for {BMS_ADDRESS}...')
            device = await BleakScanner.find_device_by_address(BMS_ADDRESS, timeout=15.0)
            if not device:
                await asyncio.sleep(5.0)
                continue

            async with BleakClient(device, timeout=20.0) as client:
                print('[launcher][bms] connected (via scan)')
                await client.start_notify(BMS_RX_CHAR, on_notify)
                await _bms_poll_session(client, packet_buf)
        except Exception as e:
            print(f'[launcher][bms] loop error: {e}')
            _latest_bms_data["connected"] = False
            if 'power' in str(e).lower():
                # e.g. bleak's "No powered Bluetooth adapters found" —
                # try to fix the actual cause instead of just sleeping and
                # hitting the exact same error again next retry.
                print('[launcher][bms] adapter appears powered off — running `bluetoothctl power on`...')
                await loop.run_in_executor(None, _try_power_on_bluetooth)
            await asyncio.sleep(5.0)


def run_bms_thread():
    """Started as a daemon thread from __main__ below. Silently does nothing
    if bleak isn't installed — /battery just keeps returning connected:false
    rather than this whole server failing to start over an optional dep."""
    try:
        import bleak  # noqa: F401 — presence check only
    except ImportError:
        print('[launcher] bleak not installed — /battery will report connected:false (pip install bleak)')
        return
    _try_power_on_bluetooth()  # covers the common case: Bluetooth was off when this server started
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(_bms_telemetry_loop())


def _sonic_python():
    """Prefer the repo-root venv actually present on this checkout — 'venv/'
    is what sonic/README.md's setup instructions actually produce (the name
    'venv', not '.venv', despite what older docs said), check both anyway
    in case a machine used the other name, else fall back to a bare
    'python3' on PATH so a machine without either venv doesn't hard-fail
    this whole file — the subprocess just fails its own import/API-key
    checks fast instead, visible via a quick 'not running' flip in
    GET /voice/status."""
    for name in ('venv', '.venv'):
        candidate = os.path.join(_ROOT, name, 'bin', 'python3')
        if os.path.isfile(candidate):
            return candidate
    return 'python3'

_NAME_RE = re.compile(r'^[A-Za-z0-9_-]+$')

def _safe_name(name):
    """Reject anything but a bare map/waypoint-set name — these come straight
    from the URL and get joined into filesystem paths, so this is the only
    thing standing between a stray '..' and reading/writing outside MAPS_DIR
    / WAYPOINTS_DIR."""
    return name if _NAME_RE.match(name or '') else None


_db_conn = None


def _db():
    """Lazily connect (and reconnect if the connection died). Returns None
    if DATABASE_URL isn't set or the connection attempt fails — every caller
    below degrades to an empty/no-op response in that case, same as the old
    file-missing behavior."""
    global _db_conn
    if not DATABASE_URL:
        return None
    if _db_conn is None or _db_conn.closed:
        try:
            _db_conn = psycopg2.connect(DATABASE_URL)
            _db_conn.autocommit = True
        except Exception as e:
            print(f'[launcher] DB connection failed: {e}')
            _db_conn = None
    return _db_conn


_location_id_cache = None


def _menu_location_id():
    """Resolves the single location_id this deployment's menu/orders belong
    to, via the same ROBOT_UID -> robots -> location_id lookup main_agent.py
    uses (see sonic/seed_db.py) — one robot, one venue, one menu/table set."""
    global _location_id_cache
    if _location_id_cache is not None:
        return _location_id_cache
    conn = _db()
    if conn is None:
        return None
    try:
        with conn.cursor() as cur:
            cur.execute('SELECT location_id FROM robots WHERE robot_uid = %s', (ROBOT_UID,))
            row = cur.fetchone()
        if row:
            _location_id_cache = str(row[0])
        return _location_id_cache
    except Exception as e:
        print(f'[launcher] location lookup failed: {e}')
        return None


def _resolve_menu_item_id(client_id):
    try:
        return str(uuid_lib.UUID(str(client_id)))
    except (ValueError, AttributeError, TypeError):
        return str(uuid_lib.uuid5(MENU_UUID_NAMESPACE, str(client_id)))


def _get_menu_response():
    """{'menu': [...], 'settings': {...}, 'savedAt': str|None} — same shape
    the old file-based /menu returned, sourced from menu_items/menu_categories/
    menu_settings instead of src/argo_mini/menu/menu.json."""
    location_id = _menu_location_id()
    conn = _db()
    if conn is None or location_id is None:
        return {'menu': [], 'settings': {}, 'savedAt': None}
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT mi.menu_item_id, mi.item_name, mi.price, mc.name,
                          mi.description, mi.image_url, mi.is_available, mi.extra
                   FROM menu_items mi
                   LEFT JOIN menu_categories mc ON mi.category_id = mc.category_id
                   WHERE mi.location_id = %s""",
                (location_id,),
            )
            rows = cur.fetchall()
            cur.execute(
                'SELECT currency_code, tax_percent, saved_at FROM menu_settings WHERE location_id = %s',
                (location_id,),
            )
            settings_row = cur.fetchone()
    except Exception as e:
        print(f'[launcher] menu read failed: {e}')
        return {'menu': [], 'settings': {}, 'savedAt': None}

    menu = []
    for menu_item_id, name, price, category, desc, image, available, extra in rows:
        extra = extra or {}
        menu.append({
            'id': str(menu_item_id),
            'name': name,
            'price': float(price),
            'category': category or 'Uncategorized',
            'desc': desc,
            'image': image,
            'available': available,
            'discountPercent': extra.get('discountPercent'),
            'discountActive': extra.get('discountActive', False),
            'eventLabel': extra.get('eventLabel'),
            'stock': extra.get('stock'),
            'diet': extra.get('diet'),
        })

    if settings_row:
        currency_code, tax_percent, saved_at = settings_row
        settings = {'currencyCode': currency_code, 'taxPercent': float(tax_percent) if tax_percent is not None else 0}
        saved_at_str = saved_at.isoformat() if saved_at else None
    else:
        settings, saved_at_str = {}, None

    return {'menu': menu, 'settings': settings, 'savedAt': saved_at_str}


def _save_menu(data):
    """Full-overwrite save (matches the old file-based POST /menu semantics:
    whatever menu.html POSTs becomes the whole menu, deletions included)."""
    location_id = _menu_location_id()
    conn = _db()
    if conn is None or location_id is None:
        return False

    category_cache = {}

    def get_or_create_category(cur, name):
        name = (name or 'Uncategorized').strip() or 'Uncategorized'
        if name in category_cache:
            return category_cache[name]
        cur.execute('SELECT category_id FROM menu_categories WHERE location_id = %s AND name = %s',
                    (location_id, name))
        row = cur.fetchone()
        if row:
            category_cache[name] = row[0]
            return row[0]
        cur.execute('INSERT INTO menu_categories (location_id, name) VALUES (%s, %s) RETURNING category_id',
                    (location_id, name))
        category_cache[name] = cur.fetchone()[0]
        return category_cache[name]

    try:
        with conn.cursor() as cur:
            seen_ids = []
            for item in data.get('menu', []):
                menu_item_id = _resolve_menu_item_id(item.get('id'))
                seen_ids.append(menu_item_id)
                category_id = get_or_create_category(cur, item.get('category'))
                extra = {
                    'discountPercent': item.get('discountPercent'),
                    'discountActive': item.get('discountActive', False),
                    'eventLabel': item.get('eventLabel'),
                    'stock': item.get('stock'),
                    'diet': item.get('diet'),
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
                    (menu_item_id, category_id, location_id, item.get('name'), item.get('desc'),
                     item.get('price', 0), item.get('available', True), item.get('image'),
                     psycopg2.extras.Json(extra)),
                )
            if seen_ids:
                cur.execute(
                    'DELETE FROM menu_items WHERE location_id = %s AND menu_item_id NOT IN %s',
                    (location_id, tuple(seen_ids)),
                )
            else:
                cur.execute('DELETE FROM menu_items WHERE location_id = %s', (location_id,))

            settings = data.get('settings') or {}
            cur.execute(
                """INSERT INTO menu_settings (location_id, currency_code, tax_percent, saved_at)
                   VALUES (%s, %s, %s, now())
                   ON CONFLICT (location_id) DO UPDATE SET
                       currency_code = EXCLUDED.currency_code,
                       tax_percent = EXCLUDED.tax_percent,
                       saved_at = EXCLUDED.saved_at""",
                (location_id, settings.get('currencyCode', 'USD'), settings.get('taxPercent', 0)),
            )
        return True
    except Exception as e:
        print(f'[launcher] menu write failed: {e}')
        return False


def _read_orders_db():
    """{table_id: {items, total, status, updatedAt}} for every service_point
    with an active visit at this deployment's one location. The map_name
    path segment callers still pass is accepted for URL compatibility with
    the frontend (which scopes by SLAM map) but not filtered on — Postgres
    models one menu/table-set per location, not per map (see seed_db.py).
    Multiple `orders` rows can exist per active visit (main_agent.py's
    db_place_order() writes one per confirm) — these are summed into a
    single per-table entry to match the old one-order-per-table file shape."""
    location_id = _menu_location_id()
    conn = _db()
    if conn is None or location_id is None:
        return {}
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT sp.label, o.order_id, o.total_amount, o.order_status, o.confirmed_at, o.placed_at
                   FROM service_points sp
                   JOIN visits v ON v.service_point_id = sp.service_point_id AND v.visit_status = 'active'
                   JOIN orders o ON o.visit_id = v.visit_id
                   WHERE sp.location_id = %s
                   ORDER BY o.placed_at""",
                (location_id,),
            )
            order_rows = cur.fetchall()
            order_ids = [r[1] for r in order_rows]
            items_by_order = {}
            if order_ids:
                cur.execute(
                    """SELECT oi.order_id, oi.menu_item_id, mi.item_name, oi.quantity, oi.unit_price
                       FROM order_items oi JOIN menu_items mi ON mi.menu_item_id = oi.menu_item_id
                       WHERE oi.order_id IN %s""",
                    (tuple(order_ids),),
                )
                for order_id, menu_item_id, name, qty, unit_price in cur.fetchall():
                    items_by_order.setdefault(order_id, []).append(
                        {'id': str(menu_item_id), 'name': name, 'qty': qty, 'price': float(unit_price)}
                    )
    except Exception as e:
        print(f'[launcher] orders read failed: {e}')
        return {}

    result = {}
    for label, order_id, total_amount, status, confirmed_at, placed_at in order_rows:
        table_id = (label or '').replace('Table ', '').strip()
        if not table_id:
            continue
        entry = result.setdefault(table_id, {'items': [], 'total': 0.0, 'status': status, 'updatedAt': None})
        entry['items'].extend(items_by_order.get(order_id, []))
        entry['total'] += float(total_amount)
        entry['status'] = status
        updated_at = confirmed_at or placed_at
        if updated_at and (entry['updatedAt'] is None or updated_at.isoformat() > entry['updatedAt']):
            entry['updatedAt'] = updated_at.isoformat()
    return result


def _clear_table_order(table_id):
    """Closes the table's active visit — the Postgres equivalent of the old
    'delete this table's order.json entry' (staff clearing a table's card),
    since orders/visits persist as history rather than living in one
    overwrite-in-place file."""
    location_id = _menu_location_id()
    conn = _db()
    if conn is None or location_id is None:
        return
    label = f'Table {table_id}'
    try:
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE visits SET visit_status = 'closed', checked_out_at = now()
                   WHERE visit_status = 'active' AND service_point_id = (
                       SELECT service_point_id FROM service_points
                       WHERE location_id = %s AND label = %s)""",
                (location_id, label),
            )
    except Exception as e:
        print(f'[launcher] order clear failed: {e}')


def _get_voice_nav_enabled():
    """Staff-facing kill-switch main_agent.py's navigate_and_wait() checks
    before every Kitchen<->Table trip. Defaults to True (including when
    DB is unset) so a missing DB doesn't read as 'disabled'."""
    location_id = _menu_location_id()
    conn = _db()
    if conn is None or location_id is None:
        return True
    try:
        with conn.cursor() as cur:
            cur.execute('SELECT voice_nav_enabled FROM locations WHERE location_id = %s', (location_id,))
            row = cur.fetchone()
            return bool(row[0]) if row else True
    except Exception as e:
        print(f'[launcher] voice_nav_enabled read failed: {e}')
        return True


def _set_voice_nav_enabled(enabled):
    location_id = _menu_location_id()
    conn = _db()
    if conn is None or location_id is None:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute('UPDATE locations SET voice_nav_enabled = %s WHERE location_id = %s',
                        (bool(enabled), location_id))
        return True
    except Exception as e:
        print(f'[launcher] voice_nav_enabled write failed: {e}')
        return False


def _read_voice_progress():
    """Reads main_agent.py's report_phase() output — {phase, text, table,
    updated_at} — same idea as /nav_progress reading NAV_PROGRESS_PATH.
    Returns (None, None) if the file is missing, unparseable, or older than
    VOICE_PROGRESS_MAX_AGE_S (a stale phase, e.g. from an uncleanly killed
    session, must never be shown as current)."""
    if not os.path.isfile(VOICE_PROGRESS_PATH):
        return None, None
    try:
        with open(VOICE_PROGRESS_PATH, 'r') as f:
            data = json.loads(f.read())
        updated_at = datetime.datetime.fromisoformat(data['updated_at'])
        age_s = (datetime.datetime.now() - updated_at).total_seconds()
        if age_s > VOICE_PROGRESS_MAX_AGE_S:
            return None, None
        return data.get('phase'), data.get('text')
    except (OSError, ValueError, KeyError, TypeError):
        return None, None


def _run_nav_bridge(destination, map_name, timeout_s=90.0):
    """Same sourced-ROS-env one-shot trip as main_agent.py's
    navigate_and_wait() — used directly by the /nav/goto background worker
    below, gated by the same staff kill-switch."""
    if not _get_voice_nav_enabled():
        print(f'[launcher] voice_nav_enabled is off — skipping /nav/goto trip to {destination!r}')
        return False
    cmd = (
        'source /opt/ros/humble/setup.bash && '
        f'source {_ROOT}/install/setup.bash && '
        'export RMW_IMPLEMENTATION=rmw_cyclonedds_cpp && '
        f'python3 {shlex.quote(NAV_BRIDGE_SCRIPT)} --map {shlex.quote(map_name)} '
        f'--destination {shlex.quote(destination)} --timeout {timeout_s}'
    )
    try:
        result = subprocess.run(['bash', '-c', cmd], capture_output=True, text=True, timeout=timeout_s + 15)
    except subprocess.TimeoutExpired:
        print(f'[launcher] /nav/goto {destination!r}: bridge process itself timed out')
        return False
    ok = result.returncode == 0 and 'RESULT:SUCCESS' in result.stdout
    print(f"[launcher] /nav/goto {destination!r}: {'arrived' if ok else 'FAILED'} (rc={result.returncode})")
    return ok


# Independent from _voice_proc above (table dispatch / wake loop) and from
# _proc above (SLAM/Nav2 stack) — a single-destination trip with no
# conversation attached, e.g. "Go to kitchen". Mutual exclusion against
# _voice_proc is enforced where each is started (POST /nav/goto and
# POST /voice/start), since both ultimately drive the same Nav2 action
# server and can never really run at once.
_nav_goto_lock = threading.Lock()
_nav_goto_status = {'running': False, 'destination': None, 'phase': None, 'phase_text': None}


def _nav_goto_worker(destination, map_name):
    global _nav_goto_status
    with _nav_goto_lock:
        _nav_goto_status = {'running': True, 'destination': destination,
                             'phase': 'heading', 'phase_text': f'Heading to {destination}'}
    ok = _run_nav_bridge(destination, map_name)
    with _nav_goto_lock:
        _nav_goto_status = {
            'running': False, 'destination': destination,
            'phase': 'arrived' if ok else 'failed',
            'phase_text': f'Arrived at {destination}' if ok else f'Could not reach {destination}',
        }


# Independent from everything above — a CPU/GPU-bound subprocess (torch),
# never touches Nav2/serial/the mic, so it needs no mutual exclusion against
# _voice_proc/_nav_goto/_proc, only against a second training job stepping
# on the same GPU/output file at once (self-exclusion via _ntfields_train_lock).
_ntfields_train_lock = threading.Lock()
_ntfields_train_status = {'running': False, 'map': None, 'phase': None, 'phase_text': None}


def _ntfields_train_worker(map_name):
    global _ntfields_train_status
    with _ntfields_train_lock:
        _ntfields_train_status = {'running': True, 'map': map_name, 'phase': 'training',
                                   'phase_text': f'Training NTFields model for "{map_name}"…'}
    os.makedirs(NTFIELDS_MODELS_DIR, exist_ok=True)
    map_yaml = os.path.join(MAPS_DIR, f'{map_name}.yaml')
    output_pt = os.path.join(NTFIELDS_MODELS_DIR, f'{map_name}.pt')
    cmd = ['python3', NTFIELDS_TRAIN_SCRIPT, '--map', map_yaml, '--output', output_pt]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
        ok = result.returncode == 0 and os.path.isfile(output_pt)
        if not ok:
            print(f'[launcher] ntfields training failed for {map_name!r}: {result.stderr[-1000:]}')
    except subprocess.TimeoutExpired:
        ok = False
        print(f'[launcher] ntfields training for {map_name!r} timed out after 1h')
    with _ntfields_train_lock:
        _ntfields_train_status = {
            'running': False, 'map': map_name,
            'phase': 'done' if ok else 'failed',
            'phase_text': f'NTFields model ready for "{map_name}"' if ok else f'Training failed for "{map_name}"',
        }

# ── State ────────────────────────────────────────────────────────────────────

_proc: subprocess.Popen | None = None
_mode: str | None = None
_map: str | None = None   # only set when _mode == 'navigate'
_lock = threading.Lock()

def _stop_locked():
    """Kill the current process group, if any. Caller must hold _lock."""
    global _proc, _mode, _map, _wake_should_run
    if _proc and _proc.poll() is None:
        try:
            os.killpg(os.getpgid(_proc.pid), signal.SIGTERM)
        except ProcessLookupError:
            pass
    _proc, _mode, _map = None, None, None
    # The wake-word loop only makes sense while Nav2 is up — stop it too
    # rather than leaving it listening (and trying to navigate) against a
    # nav stack that no longer exists.
    with _wake_lock:
        _wake_should_run = False
        _wake_stop_locked()


# Independent from _proc/_mode/_map/_lock above on purpose — a voice session
# starting/stopping must never contend with or depend on the SLAM/Nav2
# stack's own state.
_voice_proc: subprocess.Popen | None = None
_voice_action: str | None = None
_voice_map: str | None = None
_voice_table: str | None = None
_voice_lock = threading.Lock()


def _voice_stop_locked():
    """Kill the current voice-session process group, if any. Caller must
    hold _voice_lock. Mirrors _stop_locked() exactly but for the
    independent Sonic subprocess."""
    global _voice_proc, _voice_action, _voice_map, _voice_table
    if _voice_proc and _voice_proc.poll() is None:
        try:
            os.killpg(os.getpgid(_voice_proc.pid), signal.SIGTERM)
        except ProcessLookupError:
            pass
    _voice_proc, _voice_action, _voice_map, _voice_table = None, None, None, None


# Continuous wake-word loop ("Hi Sonic") — main_agent.py run with no
# TABLE_NO, auto-started once Nav2 reports READY (see
# _watch_for_nav_ready, spawned from POST /start's navigate branch) and
# auto-stopped when the nav stack stops (_stop_locked). It shares the same
# mic/speaker as a table dispatch (_voice_proc above) and can't run
# alongside one, so POST /voice/start pauses it for the dispatch's
# duration and a background thread restarts it once that dispatch process
# exits — see the pause/resume block there. _wake_should_run is separate
# from "is _wake_proc alive right now": it's the standing intent ("nav is
# up, so the loop should be running whenever nothing else needs the mic"),
# independent of it being briefly paused for a dispatch.
_wake_proc: subprocess.Popen | None = None
_wake_should_run = False
_wake_lock = threading.Lock()
# Lock ordering: _lock/_voice_lock are always acquired BEFORE _wake_lock,
# never the reverse, so pausing/resuming the wake loop from within either
# of those sections can never deadlock against it.


def _wake_start_locked():
    """Start the continuous wake-word loop, if not already running. Caller
    must hold _wake_lock."""
    global _wake_proc
    if _wake_proc and _wake_proc.poll() is None:
        return
    if not os.path.isfile(SONIC_SCRIPT):
        print(f'[launcher] wake-word loop not started — script not found: {SONIC_SCRIPT}')
        return
    try:
        with open(VOICE_LOG_PATH, 'a'):
            pass
        os.chmod(VOICE_LOG_PATH, 0o666)
        log_f = open(VOICE_LOG_PATH, 'a')
        log_f.write(f"\n{'='*70}\n[{datetime.datetime.now().isoformat()}] "
                    f"wake-word loop starting\n{'='*70}\n")
        log_f.flush()
    except OSError:
        log_f = subprocess.DEVNULL
    _wake_proc = subprocess.Popen(
        [_sonic_python(), SONIC_SCRIPT],   # no TABLE_NO -> continuous wake-word mode
        cwd=SONIC_DIR,
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=log_f,
        stderr=log_f,
        env=os.environ.copy(),
    )
    if log_f is not subprocess.DEVNULL:
        log_f.close()
    print(f'[launcher] wake-word loop started  pid={_wake_proc.pid}')


def _wake_stop_locked():
    """Stop the wake-word loop, if running. Caller must hold _wake_lock."""
    global _wake_proc
    if _wake_proc and _wake_proc.poll() is None:
        try:
            os.killpg(os.getpgid(_wake_proc.pid), signal.SIGTERM)
        except ProcessLookupError:
            pass
    _wake_proc = None


def _watch_for_nav_ready(nav_proc, started_at):
    """Background thread (one per POST /start navigate call): polls
    NAV_PROGRESS_PATH until THIS run reports READY, then auto-starts the
    wake-word loop. `started_at` guards against a stale READY left over
    from a previous run still sitting in the (shared) progress file at the
    moment this thread starts polling. Exits without starting anything if
    the nav process dies first, or if it's superseded by a stop/restart
    before ever reporting READY."""
    while nav_proc.poll() is None:
        if os.path.isfile(NAV_PROGRESS_PATH):
            try:
                with open(NAV_PROGRESS_PATH, 'r') as f:
                    raw = f.read().strip()
                bits = raw.split('|', 2)
                status = bits[0] if len(bits) == 3 else None
                ts = int(bits[1]) if len(bits) == 3 else 0
            except (OSError, ValueError):
                status, ts = None, 0
            if status == 'READY' and ts >= started_at:
                with _lock:
                    if _proc is not nav_proc or _mode != 'navigate':
                        return  # superseded — don't race a stop/restart
                global _wake_should_run
                with _wake_lock:
                    _wake_should_run = True
                    _wake_start_locked()
                print('[launcher] Nav2 READY — wake-word loop auto-started')
                return
        time.sleep(1.0)


# Independent again, on purpose — an operator hitting emergency-stop on the
# motor driver must work regardless of whatever state the nav stack or voice
# session are in, and must never be blocked waiting on either of their locks.
# serial_bridge isn't tracked as its own subprocess by this file under normal
# operation (it's one of many children argo_sonic_nav.py itself launches and
# tracks), so /estop can't just kill "the" tracked Popen — it kills by name,
# matching this file's own established pkill convention elsewhere. /estop/resume
# then relaunches it directly (bypassing the rest of the nav stack entirely),
# so an operator can recover from a stuck/misbehaving motor driver without
# having to restart the whole stack (SLAM, planner, etc. all keep running).
_serial_proc: subprocess.Popen | None = None
_serial_estopped = False
_serial_lock = threading.Lock()
SERIAL_PORT = '/dev/ttyUSB1'
SERIAL_BAUD = 115200
SERIAL_LEFT_TICK_SCALE = 0.66


def _estop_locked():
    """Send explicit zero-velocity commands before killing serial_bridge —
    confirmed as a real bug otherwise: the ESP32 firmware just keeps
    executing whatever RPM it last received (serial_bridge.py has no
    hardware-side timeout of its own), and serial_bridge's OWN safety net
    for this — its 1s watchdog timer, and the `ser.write(b"S\\n")` in
    main()'s finally: block on clean shutdown — both live inside the
    process being killed. SIGKILL can't be caught, so neither ever runs;
    the motors just kept going after the process died.

    A SINGLE zero Twist isn't enough either: serial_bridge.py ramps
    commanded RPM toward the target by at most _RPM_RAMP (5.0) per message
    received, not per second, so one message only steps speed down a
    little — confirmed the same way via TeleopPad.jsx's stopNow()/release()
    having the identical bug. `-r 10 -t 12` sends 12 zero commands at 10Hz
    (~1.2s) in one process, enough to ramp down from VMAX (~50 RPM) to
    genuine zero regardless of current speed, before serial_bridge is
    killed. Caller must hold _serial_lock."""
    global _serial_proc, _serial_estopped
    zero_cmd = (
        "source /opt/ros/humble/setup.bash && "
        f"source {_ROOT}/install/setup.bash && "
        "export RMW_IMPLEMENTATION=rmw_cyclonedds_cpp && "
        "ros2 topic pub -r 10 -t 12 /cmd_vel geometry_msgs/msg/Twist "
        "'{linear: {x: 0.0, y: 0.0, z: 0.0}, angular: {x: 0.0, y: 0.0, z: 0.0}}'"
    )
    try:
        subprocess.run(['bash', '-c', zero_cmd], capture_output=True, timeout=8)
    except subprocess.TimeoutExpired:
        pass  # still fall through to the kill below regardless
    subprocess.run(['pkill', '-9', '-f', 'serial_bridge'], capture_output=True)
    _serial_proc = None
    _serial_estopped = True


def _estop_resume_locked():
    """Relaunch serial_bridge directly, sourcing the same ROS environment
    argo_sonic_nav.py's own build_env() does. Caller must hold _serial_lock."""
    global _serial_proc, _serial_estopped
    cmd = (
        "source /opt/ros/humble/setup.bash && "
        f"source {_ROOT}/install/setup.bash && "
        "export RMW_IMPLEMENTATION=rmw_cyclonedds_cpp && "
        "ros2 run argo_mini serial_bridge --ros-args "
        f"-p port:={SERIAL_PORT} -p baud:={SERIAL_BAUD} "
        f"-p left_tick_scale:={SERIAL_LEFT_TICK_SCALE}"
    )
    _serial_proc = subprocess.Popen(
        ['bash', '-c', cmd],
        cwd=_ROOT,
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    _serial_estopped = False


# ── HTTP handler ─────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):

    def do_OPTIONS(self):
        self._cors(200)

    def do_GET(self):
        parts = [p for p in self.path.split('?')[0].split('/') if p]

        if self.path == '/status':
            with _lock:
                running = _proc is not None and _proc.poll() is None
                pid     = _proc.pid if running else None
                mode    = _mode if running else None
                map_    = _map if running else None
            self._json({'running': running, 'pid': pid, 'mode': mode, 'map': map_})

        elif self.path == '/nav_progress':
            status, message, timestamp = None, None, None
            if os.path.isfile(NAV_PROGRESS_PATH):
                with open(NAV_PROGRESS_PATH, 'r') as f:
                    raw = f.read().strip()
                # status|epoch_seconds|message — see argo_sonic_nav.py's
                # report_progress() (or sh/start_argo_nav_ui.sh's
                # report()/report_error()/report_ready()) for the writer.
                parts_ = raw.split('|', 2)
                if len(parts_) == 3:
                    status, ts_str, message = parts_
                    try:
                        timestamp = int(ts_str)
                    except ValueError:
                        timestamp = None
            self._json({'status': status, 'message': message, 'timestamp': timestamp})

        elif self.path == '/nav_log':
            log = ''
            if os.path.isfile(NAV_LOG_PATH):
                with open(NAV_LOG_PATH, 'r', errors='replace') as f:
                    lines = f.readlines()
                log = ''.join(lines[-NAV_LOG_TAIL_LINES:])
            self._json({'log': log})

        elif self.path == '/config':
            self._json({'maps_dir': MAPS_DIR})

        elif self.path == '/maps':
            names = sorted(
                os.path.splitext(f)[0]
                for f in os.listdir(MAPS_DIR)
                if f.endswith('.yaml')
            ) if os.path.isdir(MAPS_DIR) else []
            self._json({'maps': names})

        elif self.path == '/ntfields_models':
            # SettingsPanel.jsx already expects exactly this shape (see its
            # "NTFields model ready"/"No NTFields model" badge) — this was
            # the missing half of that wiring.
            names = sorted(
                os.path.splitext(f)[0]
                for f in os.listdir(NTFIELDS_MODELS_DIR)
                if f.endswith('.pt')
            ) if os.path.isdir(NTFIELDS_MODELS_DIR) else []
            self._json({'models': names})

        elif len(parts) == 3 and parts[0] == 'maps' and parts[2] == 'meta':
            name = _safe_name(parts[1])
            if not name:
                self._json({'error': 'invalid map name'}, 400)
                return
            yaml_path = os.path.join(MAPS_DIR, f'{name}.yaml')
            if not os.path.isfile(yaml_path):
                self._json({'error': 'not found'}, 404)
                return
            # Hand-rolled parse of just the two fields we need — map_saver's
            # yaml is a fixed, simple format (flat key: value, one inline
            # array), so a real YAML dependency isn't worth adding for this.
            resolution, origin = 0.05, [0.0, 0.0, 0.0]
            with open(yaml_path, 'r') as f:
                for line in f:
                    line = line.strip()
                    if line.startswith('resolution:'):
                        try:
                            resolution = float(line.split(':', 1)[1].strip())
                        except ValueError:
                            pass
                    elif line.startswith('origin:'):
                        raw = line.split(':', 1)[1].strip().strip('[]')
                        try:
                            origin = [float(v.strip()) for v in raw.split(',')]
                        except ValueError:
                            pass
            self._json({'resolution': resolution, 'origin': origin})

        elif len(parts) == 3 and parts[0] == 'maps' and parts[2] == 'preview':
            name = _safe_name(parts[1])
            if not name:
                self._json({'error': 'invalid map name'}, 400)
                return
            pgm_path = os.path.join(MAPS_DIR, f'{name}.pgm')
            if not os.path.isfile(pgm_path):
                self._json({'error': 'not found'}, 404)
                return
            with open(pgm_path, 'rb') as f:
                data = f.read()
            self.send_response(200)
            self.send_header('Content-Type', 'application/octet-stream')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self._write(data)

        elif len(parts) == 2 and parts[0] == 'waypoints':
            name = _safe_name(parts[1])
            if not name:
                self._json({'error': 'invalid map name'}, 400)
                return
            wp_path = os.path.join(WAYPOINTS_DIR, f'{name}.json')
            if os.path.isfile(wp_path):
                with open(wp_path, 'r') as f:
                    try:
                        data = json.load(f)
                    except json.JSONDecodeError:
                        data = {}
            else:
                data = {}
            self._json(data)

        elif self.path == '/menu':
            self._json(_get_menu_response())

        elif len(parts) == 2 and parts[0] == 'orders':
            name = _safe_name(parts[1])
            if not name:
                self._json({'error': 'invalid map name'}, 400)
                return
            self._json(_read_orders_db())

        elif len(parts) == 3 and parts[0] == 'orders':
            name = _safe_name(parts[1])
            table_id = _safe_name(parts[2])
            if not name or not table_id:
                self._json({'error': 'invalid map name or table id'}, 400)
                return
            self._json(_read_orders_db().get(table_id, {}))

        elif self.path == '/voice/status':
            with _voice_lock:
                running = _voice_proc is not None and _voice_proc.poll() is None
                pid     = _voice_proc.pid if running else None
                action  = _voice_action if running else None
                map_    = _voice_map if running else None
                table   = _voice_table if running else None
            with _wake_lock:
                wake_running = _wake_proc is not None and _wake_proc.poll() is None
                wake_pending = _wake_should_run and not wake_running  # paused for a dispatch, or still starting up
            phase, phase_text = _read_voice_progress()
            self._json({'running': running, 'pid': pid, 'action': action, 'map': map_, 'table': table,
                        'wake_loop_running': wake_running, 'wake_loop_pending': wake_pending,
                        'phase': phase, 'phase_text': phase_text})

        elif self.path == '/voice/nav_enabled':
            self._json({'enabled': _get_voice_nav_enabled()})

        elif self.path == '/nav/goto/status':
            with _nav_goto_lock:
                self._json(dict(_nav_goto_status))

        elif self.path == '/ntfields/train/status':
            with _ntfields_train_lock:
                self._json(dict(_ntfields_train_status))

        elif self.path == '/estop/status':
            with _serial_lock:
                self._json({'estopped': _serial_estopped})

        elif self.path == '/battery':
            self._json(dict(_latest_bms_data))

        else:
            self._json({'error': 'not found'}, 404)

    def do_POST(self):
        global _proc, _mode, _map

        if self.path == '/start':
            mode = 'auto'
            map_name = None
            try:
                length = int(self.headers.get('Content-Length', 0))
                if length:
                    body = json.loads(self.rfile.read(length))
                    if body.get('mode') in ('manual', 'navigate'):
                        mode = body['mode']
                    map_name = _safe_name(body.get('map'))
            except (ValueError, json.JSONDecodeError):
                pass

            if mode == 'navigate':
                if not map_name:
                    self._json({'ok': False, 'error': 'navigate mode requires a valid "map" name'}, 400)
                    return
                script = NAV_SCRIPT
                args = ['python3', script, '--map', os.path.join(MAPS_DIR, map_name), '--no-rviz']
            else:
                script = SLAM_SCRIPT if mode == 'manual' else EXPLORE_SCRIPT
                args = ['bash', script, '--no-rviz']
                map_name = None

            with _lock:
                already = _proc and _proc.poll() is None
                if already and _mode == mode and _map == map_name:
                    self._json({'ok': True, 'status': 'already_running', 'pid': _proc.pid, 'mode': _mode, 'map': _map})
                    return
                if already:
                    # Different mode or map requested — e.g. switching which map to
                    # navigate on. Stop the old stack first so we never end up
                    # navigating against the wrong map's coordinate frame.
                    print(f'[launcher] switching stack (was mode={_mode} map={_map}) — stopping first')
                    _stop_locked()
                if not os.path.isfile(script):
                    self._json({'ok': False, 'error': f'script not found: {script}'}, 500)
                    return
                # This service runs as root, so a freshly-created progress
                # file defaults to root-only-writable — fine for this
                # process, but it then blocks anyone running the same
                # script by hand (as themselves, over SSH) from writing to
                # it at all, since /tmp's sticky bit means only root or the
                # owner can even chmod it after the fact. Force it open
                # every time so a later by-hand debugging run never hits
                # that silently.
                try:
                    with open(NAV_PROGRESS_PATH, 'a'):
                        pass
                    os.chmod(NAV_PROGRESS_PATH, 0o666)
                except OSError:
                    pass
                # Fresh log file per run ('w', not 'a') — otherwise GET
                # /nav_log would show a previous run's output stitched onto
                # this one with no clear boundary. Same cross-user
                # permission reasoning as NAV_PROGRESS_PATH above.
                try:
                    with open(NAV_LOG_PATH, 'w'):
                        pass
                    os.chmod(NAV_LOG_PATH, 0o666)
                except OSError:
                    pass
                nav_start_ts = int(time.time())
                nav_log_f = open(NAV_LOG_PATH, 'a')
                _proc = subprocess.Popen(
                    # --no-rviz: the robot is headless and driven entirely
                    # through this web UI — rviz2 has no DISPLAY to attach
                    # to, fails instantly, and the script's own trailing
                    # `wait $RVIZ_PID` would then return immediately, making
                    # this wrapper process exit while its background ROS
                    # nodes keep running as orphans. That desyncs /status
                    # (reports "stopped") from reality (stack still up).
                    args,
                    cwd=_ROOT,
                    start_new_session=True,   # own process group → clean kill
                    stdout=nav_log_f,
                    stderr=nav_log_f,
                )
                nav_log_f.close()  # child has its own fd via dup2; safe to close our copy now
                _mode = mode
                _map = map_name
                if mode == 'navigate':
                    # Auto-start the wake-word loop once THIS run reports
                    # Nav2 READY, so a guest can say "Hi Sonic" without
                    # staff separately SSH-ing in to start it by hand.
                    threading.Thread(
                        target=_watch_for_nav_ready, args=(_proc, nav_start_ts), daemon=True,
                    ).start()
            print(f'[launcher] stack started  mode={mode}  map={map_name}  pid={_proc.pid}')
            self._json({'ok': True, 'status': 'started', 'pid': _proc.pid, 'mode': mode, 'map': map_name})

        elif self.path == '/stop':
            with _lock:
                _stop_locked()
            print('[launcher] stack stopped')
            self._json({'ok': True, 'status': 'stopped'})

        elif self.path.startswith('/waypoints/'):
            name = _safe_name(self.path[len('/waypoints/'):])
            if not name:
                self._json({'error': 'invalid map name'}, 400)
                return
            try:
                length = int(self.headers.get('Content-Length', 0))
                data = json.loads(self.rfile.read(length)) if length else {}
            except (ValueError, json.JSONDecodeError):
                self._json({'error': 'invalid JSON body'}, 400)
                return
            os.makedirs(WAYPOINTS_DIR, exist_ok=True)
            wp_path = os.path.join(WAYPOINTS_DIR, f'{name}.json')
            with open(wp_path, 'w') as f:
                json.dump(data, f, indent=2)
            self._json({'ok': True})

        elif self.path == '/menu':
            try:
                length = int(self.headers.get('Content-Length', 0))
                data = json.loads(self.rfile.read(length)) if length else {}
            except (ValueError, json.JSONDecodeError):
                self._json({'error': 'invalid JSON body'}, 400)
                return
            if not _save_menu(data):
                self._json({'error': 'menu write failed — check DATABASE_URL / launcher logs'}, 500)
                return
            self._json({'ok': True})

        elif self.path.startswith('/orders/'):
            rest = self.path[len('/orders/'):].split('/')
            if len(rest) != 2:
                self._json({'error': 'expected /orders/<map_name>/<table_id>'}, 400)
                return
            name = _safe_name(rest[0])
            table_id = _safe_name(rest[1])
            if not name or not table_id:
                self._json({'error': 'invalid map name or table id'}, 400)
                return
            # No frontend caller writes here (orders are written directly by
            # main_agent.py's db_place_order() as the guest orders) — kept
            # as a 501 rather than silently dropped, in case something else
            # starts depending on it.
            self._json({'error': 'orders are now written by the voice agent (db_place_order) — '
                                  'POST /orders/<map>/<table> is not supported'}, 501)

        elif self.path == '/voice/start':
            global _voice_proc, _voice_action, _voice_map, _voice_table
            try:
                length = int(self.headers.get('Content-Length', 0))
                body = json.loads(self.rfile.read(length)) if length else {}
            except (ValueError, json.JSONDecodeError):
                self._json({'ok': False, 'error': 'invalid JSON body'}, 400)
                return

            action   = body.get('action')
            map_name = _safe_name(body.get('map'))
            # Not run through _safe_name — that's for names that get joined
            # into filesystem paths (map/waypoint-set files). table_id here
            # is only ever passed as an env var, compared in memory, and
            # logged, so it's taken as-is from the waypoint's own name (e.g.
            # "Table 3", "table 1") straight off the table card — no
            # character restrictions needed, just non-empty.
            table_id = str(body.get('table', '')).strip()

            if action not in _VOICE_ACTIONS:
                self._json({'ok': False, 'error': f'invalid action — must be one of {sorted(_VOICE_ACTIONS)}'}, 400)
                return
            if not map_name or not table_id:
                self._json({'ok': False, 'error': 'invalid or missing "map"/"table"'}, 400)
                return

            with _nav_goto_lock:
                if _nav_goto_status['running']:
                    # Same physical robot, same Nav2 action server — a plain
                    # /nav/goto trip (e.g. "Go to kitchen") and a table
                    # dispatch can never run at once.
                    self._json({'ok': False, 'error': 'nav_goto_busy',
                                'active': {'destination': _nav_goto_status['destination']}}, 409)
                    return

            with _voice_lock:
                already = _voice_proc and _voice_proc.poll() is None
                if already and _voice_action == action and _voice_map == map_name and _voice_table == table_id:
                    self._json({'ok': True, 'status': 'already_running', 'pid': _voice_proc.pid,
                                'action': action, 'map': map_name, 'table': table_id})
                    return
                if already:
                    # A DIFFERENT table/action is active — reject rather than
                    # pre-empt (unlike /start's stop-then-restart): Sonic can
                    # only physically speak with one table at a time, so
                    # silently killing an in-progress order to start a new
                    # one would lose that customer's conversation.
                    self._json({
                        'ok': False, 'error': 'voice_session_busy',
                        'active': {'action': _voice_action, 'map': _voice_map,
                                   'table': _voice_table, 'pid': _voice_proc.pid},
                    }, 409)
                    return
                if not os.path.isfile(SONIC_SCRIPT):
                    self._json({'ok': False, 'error': f'script not found: {SONIC_SCRIPT}'}, 500)
                    return

                # main_agent.py has no --table/--map/--action CLI flags (it's
                # a continuous wake-word loop that discovers the table
                # conversationally) — TABLE_NO tells it to skip that
                # discovery and run one real Kitchen->Table->Kitchen round
                # trip for this table (nav_bridge.py sends the actual Nav2
                # goals), preserving the old click-to-dispatch UX.
                # SONIC_ACTION_HINT selects order/room_service (round trip +
                # full humanized take-order conversation) vs. bill/deliver,
                # which currently just get a brief spoken apology and a
                # return to the kitchen (n_stub — not yet rewired into
                # main_agent.py). SONIC_MAP_NAME picks which waypoints file
                # to navigate against.
                args = [_sonic_python(), SONIC_SCRIPT]
                # Appended, not overwritten — so a prior session's output is
                # still there to compare against. A clear separator per
                # session is enough to tell them apart without needing log
                # rotation. DEVNULL previously swallowed this entirely, which
                # made "why did the session end with no order?" undiagnosable
                # from either side (crash? exception? no mic hardware?).
                #
                # Confirmed real bug on argo-desktop: this file was created
                # back when argo-launcher ran as root (before the fix for the
                # ntfields_models path issue switched it to User=argo), so it
                # was root-owned and unwritable by 'argo' — open() raised an
                # uncaught PermissionError here, which aborted this one
                # request's connection ungracefully (the server itself kept
                # running fine for everything else). The browser then saw a
                # broken connection and reported "Could not reach launcher"
                # for what was actually a file-permission problem several
                # layers away. chmod after creating/opening it once (same
                # pattern as NAV_PROGRESS_PATH/NAV_LOG_PATH above) fixes this
                # from recurring; falling back to DEVNULL if that still fails
                # means a logging permission hiccup can never block Sonic
                # from actually starting.
                try:
                    with open(VOICE_LOG_PATH, 'a'):
                        pass
                    os.chmod(VOICE_LOG_PATH, 0o666)
                    log_f = open(VOICE_LOG_PATH, 'a')
                    log_f.write(f"\n{'='*70}\n[{datetime.datetime.now().isoformat()}] "
                                f"action={action} map={map_name} table={table_id}\n{'='*70}\n")
                    log_f.flush()
                except OSError:
                    log_f = subprocess.DEVNULL
                # The wake-word loop and a table dispatch share the same
                # mic/speaker and can't run at once — pause it for the
                # dispatch's duration; a background thread below restarts
                # it once this dispatch process exits (if it's still
                # supposed to be running — i.e. nav hasn't been stopped
                # meanwhile).
                with _wake_lock:
                    _wake_stop_locked()
                _voice_proc = subprocess.Popen(
                    args,
                    cwd=SONIC_DIR,             # sonic/*.py use bare `import config` etc, not package-relative
                    start_new_session=True,    # own process group → clean kill, mirrors SLAM stack
                    stdin=subprocess.DEVNULL,  # no TTY
                    stdout=log_f,
                    stderr=log_f,
                    env={**os.environ, 'TABLE_NO': table_id, 'SONIC_ACTION_HINT': action,
                         'SONIC_MAP_NAME': map_name},
                )
                if log_f is not subprocess.DEVNULL:
                    log_f.close()  # child has its own fd via dup2; safe to close our copy now
                _voice_action, _voice_map, _voice_table = action, map_name, table_id

                def _resume_wake_loop_after(dispatch_proc):
                    dispatch_proc.wait()
                    with _wake_lock:
                        if _wake_should_run:
                            _wake_start_locked()
                threading.Thread(
                    target=_resume_wake_loop_after, args=(_voice_proc,), daemon=True,
                ).start()
            print(f'[launcher] voice session started  action={action}  map={map_name}  table={table_id}  pid={_voice_proc.pid}')
            self._json({'ok': True, 'status': 'started', 'pid': _voice_proc.pid,
                        'action': action, 'map': map_name, 'table': table_id})

        elif self.path == '/voice/stop':
            with _voice_lock:
                _voice_stop_locked()
            print('[launcher] voice session stopped')
            self._json({'ok': True, 'status': 'stopped'})

        elif self.path == '/voice/nav_enabled':
            try:
                length = int(self.headers.get('Content-Length', 0))
                body = json.loads(self.rfile.read(length)) if length else {}
            except (ValueError, json.JSONDecodeError):
                self._json({'ok': False, 'error': 'invalid JSON body'}, 400)
                return
            if 'enabled' not in body:
                self._json({'ok': False, 'error': 'missing "enabled"'}, 400)
                return
            if not _set_voice_nav_enabled(body['enabled']):
                self._json({'ok': False, 'error': 'write failed — check DATABASE_URL / launcher logs'}, 500)
                return
            print(f"[launcher] voice-triggered navigation {'enabled' if body['enabled'] else 'disabled'}")
            self._json({'ok': True, 'enabled': bool(body['enabled'])})

        elif self.path == '/nav/goto':
            try:
                length = int(self.headers.get('Content-Length', 0))
                body = json.loads(self.rfile.read(length)) if length else {}
            except (ValueError, json.JSONDecodeError):
                self._json({'ok': False, 'error': 'invalid JSON body'}, 400)
                return

            destination = _safe_name(body.get('destination'))
            map_name    = _safe_name(body.get('map'))
            if not destination or not map_name:
                self._json({'ok': False, 'error': 'invalid or missing "destination"/"map"'}, 400)
                return

            with _voice_lock:
                voice_busy = _voice_proc and _voice_proc.poll() is None
            if voice_busy:
                # Same reasoning as /voice/start's own busy check, the other
                # way around — a table dispatch already owns the robot.
                self._json({'ok': False, 'error': 'voice_session_busy',
                            'active': {'action': _voice_action, 'table': _voice_table}}, 409)
                return

            with _nav_goto_lock:
                if _nav_goto_status['running']:
                    self._json({'ok': False, 'error': 'nav_goto_busy',
                                'active': {'destination': _nav_goto_status['destination']}}, 409)
                    return
                threading.Thread(target=_nav_goto_worker, args=(destination, map_name), daemon=True).start()
            print(f'[launcher] /nav/goto -> {destination!r} (map={map_name!r})')
            self._json({'ok': True, 'status': 'started', 'destination': destination, 'map': map_name})

        elif self.path == '/ntfields/train':
            try:
                length = int(self.headers.get('Content-Length', 0))
                body = json.loads(self.rfile.read(length)) if length else {}
            except (ValueError, json.JSONDecodeError):
                self._json({'ok': False, 'error': 'invalid JSON body'}, 400)
                return

            map_name = _safe_name(body.get('map'))
            if not map_name:
                self._json({'ok': False, 'error': 'invalid or missing "map"'}, 400)
                return
            if not os.path.isfile(os.path.join(MAPS_DIR, f'{map_name}.yaml')):
                self._json({'ok': False, 'error': f'no saved map named {map_name!r}'}, 404)
                return

            with _ntfields_train_lock:
                if _ntfields_train_status['running']:
                    self._json({'ok': False, 'error': 'ntfields_train_busy',
                                'active': {'map': _ntfields_train_status['map']}}, 409)
                    return
                threading.Thread(target=_ntfields_train_worker, args=(map_name,), daemon=True).start()
            print(f'[launcher] /ntfields/train -> {map_name!r}')
            self._json({'ok': True, 'status': 'started', 'map': map_name})

        elif self.path == '/estop':
            with _serial_lock:
                _estop_locked()
            print('[launcher] EMERGENCY STOP — serial_bridge killed')
            self._json({'ok': True, 'status': 'estopped'})

        elif self.path == '/estop/resume':
            with _serial_lock:
                _estop_resume_locked()
            print(f'[launcher] serial_bridge resumed  pid={_serial_proc.pid}')
            self._json({'ok': True, 'status': 'resumed', 'pid': _serial_proc.pid})

        else:
            self._json({'error': 'not found'}, 404)

    def do_DELETE(self):
        if self.path.startswith('/orders/'):
            rest = self.path[len('/orders/'):].split('/')
            if len(rest) != 2:
                self._json({'error': 'expected /orders/<map_name>/<table_id>'}, 400)
                return
            name = _safe_name(rest[0])
            table_id = _safe_name(rest[1])
            if not name or not table_id:
                self._json({'error': 'invalid map name or table id'}, 400)
                return
            _clear_table_order(table_id)
            self._json({'ok': True})

        else:
            self._json({'error': 'not found'}, 404)

    # ── helpers ──────────────────────────────────────────────────────────────

    def _json(self, data: dict, code: int = 200):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header('Content-Type',  'application/json')
        self.send_header('Access-Control-Allow-Origin',  '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, DELETE, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self._write(body)

    def _write(self, data: bytes):
        """Client disconnected (tab closed, a poll superseded by a newer
        one, page navigated away) before we finished writing — harmless and
        expected under the dashboard's own polling load, not a real error.
        BaseHTTPRequestHandler otherwise dumps a full traceback to the
        console for this every single time; route it through log_message
        instead so it's suppressed the same as every other per-request line
        (see log_message's override below), rather than printed raw."""
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            self.log_message('client disconnected before response finished (%s)', self.path)

    def _cors(self, code: int):
        self.send_response(code)
        self.send_header('Access-Control-Allow-Origin',  '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, DELETE, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()

    def log_message(self, *_):
        pass   # silence per-request console noise


# ── Entry point ──────────────────────────────────────────────────────────────

if __name__ == '__main__':
    print(f'[launcher] manual script   → {SLAM_SCRIPT}')
    print(f'[launcher] auto script     → {EXPLORE_SCRIPT}')
    print(f'[launcher] navigate script → {NAV_SCRIPT}')
    print(f'[launcher] maps dir        → {MAPS_DIR}')
    print(f'[launcher] waypoints dir   → {WAYPOINTS_DIR}')
    if not os.path.isfile(SLAM_SCRIPT):
        print(f'[launcher] WARNING: manual script not found — check path above')
    if not os.path.isfile(EXPLORE_SCRIPT):
        print(f'[launcher] WARNING: auto script not found — check path above')
    if not os.path.isfile(NAV_SCRIPT):
        print(f'[launcher] WARNING: navigate script not found — check path above')
    os.makedirs(MAPS_DIR, exist_ok=True)
    os.makedirs(WAYPOINTS_DIR, exist_ok=True)
    if not DATABASE_URL:
        print('[launcher] WARNING: DATABASE_URL not set (sonic/.env) — /menu and /orders will return empty')
    print(f'[launcher] sonic interpreter → {_sonic_python()}')
    print(f'[launcher] voice session log → {VOICE_LOG_PATH}')
    if not os.path.isfile(SONIC_SCRIPT):
        print(f'[launcher] WARNING: main_agent.py not found — check path above')
    threading.Thread(target=run_bms_thread, daemon=True).start()
    server = HTTPServer(('0.0.0.0', PORT), Handler)
    print(f'[launcher] listening on  http://0.0.0.0:{PORT}')
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('[launcher] shutting down')
