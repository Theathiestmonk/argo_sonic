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
    GET  /maps/<name>/meta         →  {"resolution": float, "origin": [x,y,theta]}
                                       parsed from <name>.yaml — lets the frontend
                                       place waypoints on the map image correctly
    GET  /maps/<name>/preview      →  raw bytes of <name>.pgm
    GET  /waypoints/<map_name>     →  JSON content of waypoints/<map_name>.json
                                       (empty {} if that map has no waypoints yet)
    POST /waypoints/<map_name>     →  body is the full waypoints dict; overwrites
                                       the file (mirrors waypoint_manager.py's own
                                       full-rewrite save_waypoints() semantics)
    GET  /menu    →  JSON content of menu/menu.json (one global menu, not per-map —
                      a venue has one menu regardless of which SLAM map is loaded).
                      Seeded once from frontend/argo-menu-backup.json on first run
                      if menu.json doesn't exist yet.
    POST /menu    →  body is {"menu": [...], "settings": {...}, "savedAt": str};
                      overwrites the file. frontend/public/menu-data.js POSTs here
                      after every localStorage save, so this file stays the live
                      mirror of whatever staff last edited in menu.html — and Sonic
                      (backend/../sonic/) reads it to check real-time availability.
    GET  /orders/<map_name>            →  JSON content of orders/<map_name>.json
                                           (empty {} if no orders yet for that map)
    GET  /orders/<map_name>/<table_id> →  just that table's order ({} if none)
    POST /orders/<map_name>/<table_id> →  body is ONE table's order object; this
                                           read-modify-writes the whole file (unlike
                                           waypoints' full-overwrite) so one table's
                                           order can never clobber another's
    DELETE /orders/<map_name>/<table_id> → removes that table's order (e.g. staff
                                           clearing history from its card); no-op
                                           (still 200) if it wasn't there
    POST /start   →  body {"mode": "manual"|"auto"|"navigate", "map": str} (default "auto")
                      "manual"   → sh/start_slam_ui.sh          (SLAM only, no Nav2 — build a map)
                      "auto"     → sh/start_slam_explore_ui.sh  (SLAM + Nav2 + frontier explorer)
                      "navigate" → sh/start_argo_nav_ui.sh --map <MAPS_DIR>/<map>
                                    (SLAM-toolbox localization + Nav2 + depth safety
                                    shield, on a previously saved map — "map" is required)
                      If something's already running under a different mode/map than
                      requested, it's stopped first so the new one always reflects what
                      was actually asked for (e.g. switching which map to navigate on).
    POST /stop    →  kills the entire process group cleanly
    GET  /nav_progress → {"status": "OK"|"ERROR"|"READY"|"STOPPED"|null,
                          "message": str|null, "timestamp": int|null} —
                          the current/last step sh/start_argo_nav_ui.sh
                          reported, read from NAV_PROGRESS_PATH. Exists
                          because launcher.py deliberately discards that
                          script's own stdout/stderr (see POST /start's
                          subprocess.DEVNULL) to keep this single-threaded
                          server from being wedged by a chatty/slow node —
                          which otherwise left the UI with nothing to show
                          but an indefinite "waiting" spinner no matter
                          what broke or how long it had been stuck.
                          "status": null means the file doesn't exist yet
                          (nothing has ever reported in).

    GET  /voice/status →  {"running": bool, "pid": int|null, "action": str|null,
                           "map": str|null, "table": str|null} — is Sonic
                           currently mid-conversation at a table?
    POST /voice/start  →  body {"action": "order"|"deliver"|"bill"|"room_service",
                           "map": str, "table": str}. Spawns sonic/test_harness.py
                           as a subprocess scoped to that table — this is what a
                           table's action buttons in the UI actually call.
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

These *_ui.sh scripts are dedicated copies of the hand-run sh/start_slam.sh
and sh/start_slam_explore.sh — kept separate so UI-driven launches never
disturb the scripts used directly over SSH.
"""

import datetime
import json
import os
import re
import signal
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

# ── Config ──────────────────────────────────────────────────────────────────

# Root of this repo (backend/ is one level below root)
_HERE   = os.path.dirname(os.path.abspath(__file__))
_ROOT   = os.path.dirname(_HERE)

SLAM_SCRIPT    = os.path.join(_ROOT, 'sh', 'start_slam_ui.sh')
EXPLORE_SCRIPT = os.path.join(_ROOT, 'sh', 'start_slam_explore_ui.sh')
NAV_SCRIPT     = os.path.join(_ROOT, 'sh', 'start_argo_nav_ui.sh')
MAPS_DIR       = os.path.join(_ROOT, 'src', 'argo_mini', 'maps')
WAYPOINTS_DIR  = os.path.join(_ROOT, 'src', 'argo_mini', 'waypoints')
MENU_DIR       = os.path.join(_ROOT, 'src', 'argo_mini', 'menu')
MENU_PATH      = os.path.join(MENU_DIR, 'menu.json')
BACKUP_MENU_PATH = os.path.join(_ROOT, 'frontend', 'argo-menu-backup.json')
ORDERS_DIR     = os.path.join(_ROOT, 'src', 'argo_mini', 'orders')
SONIC_DIR            = os.path.join(_ROOT, 'sonic')
TEST_HARNESS_SCRIPT  = os.path.join(SONIC_DIR, 'test_harness.py')
VOICE_LOG_PATH       = os.path.join(SONIC_DIR, 'voice_session.log')
_VOICE_ACTIONS = {'order', 'deliver', 'bill', 'room_service'}
NAV_PROGRESS_PATH = '/tmp/argo_nav_progress'  # written by sh/start_argo_nav_ui.sh
PORT    = 8888


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


def _seed_menu_if_missing():
    """One-time copy of the stale manual export into the real, live menu file
    — after this, frontend/public/menu-data.js POSTs here on every save, so
    BACKUP_MENU_PATH is never read again post-seed."""
    if os.path.isfile(MENU_PATH):
        return
    os.makedirs(MENU_DIR, exist_ok=True)
    if os.path.isfile(BACKUP_MENU_PATH):
        try:
            with open(BACKUP_MENU_PATH, 'r') as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            data = {'menu': [], 'settings': {}, 'savedAt': None}
    else:
        data = {'menu': [], 'settings': {}, 'savedAt': None}
    with open(MENU_PATH, 'w') as f:
        json.dump(data, f, indent=2)


def _read_orders(map_name):
    path = os.path.join(ORDERS_DIR, f'{map_name}.json')
    if not os.path.isfile(path):
        return {}
    with open(path, 'r') as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            return {}

# ── State ────────────────────────────────────────────────────────────────────

_proc: subprocess.Popen | None = None
_mode: str | None = None
_map: str | None = None   # only set when _mode == 'navigate'
_lock = threading.Lock()

def _stop_locked():
    """Kill the current process group, if any. Caller must hold _lock."""
    global _proc, _mode, _map
    if _proc and _proc.poll() is None:
        try:
            os.killpg(os.getpgid(_proc.pid), signal.SIGTERM)
        except ProcessLookupError:
            pass
    _proc, _mode, _map = None, None, None


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
                # status|epoch_seconds|message — see sh/start_argo_nav_ui.sh's
                # report()/report_error()/report_ready() helpers for the writer.
                parts_ = raw.split('|', 2)
                if len(parts_) == 3:
                    status, ts_str, message = parts_
                    try:
                        timestamp = int(ts_str)
                    except ValueError:
                        timestamp = None
            self._json({'status': status, 'message': message, 'timestamp': timestamp})

        elif self.path == '/config':
            self._json({'maps_dir': MAPS_DIR})

        elif self.path == '/maps':
            names = sorted(
                os.path.splitext(f)[0]
                for f in os.listdir(MAPS_DIR)
                if f.endswith('.yaml')
            ) if os.path.isdir(MAPS_DIR) else []
            self._json({'maps': names})

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
            self.wfile.write(data)

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
            if os.path.isfile(MENU_PATH):
                with open(MENU_PATH, 'r') as f:
                    try:
                        data = json.load(f)
                    except json.JSONDecodeError:
                        data = {'menu': [], 'settings': {}, 'savedAt': None}
            else:
                data = {'menu': [], 'settings': {}, 'savedAt': None}
            self._json(data)

        elif len(parts) == 2 and parts[0] == 'orders':
            name = _safe_name(parts[1])
            if not name:
                self._json({'error': 'invalid map name'}, 400)
                return
            self._json(_read_orders(name))

        elif len(parts) == 3 and parts[0] == 'orders':
            name = _safe_name(parts[1])
            table_id = _safe_name(parts[2])
            if not name or not table_id:
                self._json({'error': 'invalid map name or table id'}, 400)
                return
            self._json(_read_orders(name).get(table_id, {}))

        elif self.path == '/voice/status':
            with _voice_lock:
                running = _voice_proc is not None and _voice_proc.poll() is None
                pid     = _voice_proc.pid if running else None
                action  = _voice_action if running else None
                map_    = _voice_map if running else None
                table   = _voice_table if running else None
            self._json({'running': running, 'pid': pid, 'action': action, 'map': map_, 'table': table})

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
                args = ['bash', script, '--map', os.path.join(MAPS_DIR, map_name), '--no-rviz']
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
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                _mode = mode
                _map = map_name
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
            os.makedirs(MENU_DIR, exist_ok=True)
            with open(MENU_PATH, 'w') as f:
                json.dump(data, f, indent=2)
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
            try:
                length = int(self.headers.get('Content-Length', 0))
                order = json.loads(self.rfile.read(length)) if length else {}
            except (ValueError, json.JSONDecodeError):
                self._json({'error': 'invalid JSON body'}, 400)
                return
            os.makedirs(ORDERS_DIR, exist_ok=True)
            all_orders = _read_orders(name)
            all_orders[table_id] = order
            orders_path = os.path.join(ORDERS_DIR, f'{name}.json')
            with open(orders_path, 'w') as f:
                json.dump(all_orders, f, indent=2)
            self._json({'ok': True})

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
            table_id = _safe_name(str(body.get('table', '')))

            if action not in _VOICE_ACTIONS:
                self._json({'ok': False, 'error': f'invalid action — must be one of {sorted(_VOICE_ACTIONS)}'}, 400)
                return
            if not map_name or not table_id:
                self._json({'ok': False, 'error': 'invalid or missing "map"/"table"'}, 400)
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
                if not os.path.isfile(TEST_HARNESS_SCRIPT):
                    self._json({'ok': False, 'error': f'script not found: {TEST_HARNESS_SCRIPT}'}, 500)
                    return

                args = [_sonic_python(), TEST_HARNESS_SCRIPT,
                        '--table', table_id, '--map', map_name, '--action', action]
                # Appended, not overwritten — so a prior session's output is
                # still there to compare against. A clear separator per
                # session is enough to tell them apart without needing log
                # rotation. DEVNULL previously swallowed this entirely, which
                # made "why did the session end with no order?" undiagnosable
                # from either side (crash? exception? no mic hardware?).
                log_f = open(VOICE_LOG_PATH, 'a')
                log_f.write(f"\n{'='*70}\n[{datetime.datetime.now().isoformat()}] "
                            f"action={action} map={map_name} table={table_id}\n{'='*70}\n")
                log_f.flush()
                _voice_proc = subprocess.Popen(
                    args,
                    cwd=SONIC_DIR,             # sonic/*.py use bare `import config` etc, not package-relative
                    start_new_session=True,    # own process group → clean kill, mirrors SLAM stack
                    stdin=subprocess.DEVNULL,  # no TTY — keyboard_loop()'s input() would otherwise
                                               # raise EOFError against a closed stream (order action
                                               # only) or, worse, fight over this process's own
                                               # terminal if left un-redirected
                    stdout=log_f,
                    stderr=log_f,
                )
                log_f.close()  # child has its own fd via dup2; safe to close our copy now
                _voice_action, _voice_map, _voice_table = action, map_name, table_id
            print(f'[launcher] voice session started  action={action}  map={map_name}  table={table_id}  pid={_voice_proc.pid}')
            self._json({'ok': True, 'status': 'started', 'pid': _voice_proc.pid,
                        'action': action, 'map': map_name, 'table': table_id})

        elif self.path == '/voice/stop':
            with _voice_lock:
                _voice_stop_locked()
            print('[launcher] voice session stopped')
            self._json({'ok': True, 'status': 'stopped'})

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
            all_orders = _read_orders(name)
            all_orders.pop(table_id, None)
            os.makedirs(ORDERS_DIR, exist_ok=True)
            orders_path = os.path.join(ORDERS_DIR, f'{name}.json')
            with open(orders_path, 'w') as f:
                json.dump(all_orders, f, indent=2)
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
        self.wfile.write(body)

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
    os.makedirs(ORDERS_DIR, exist_ok=True)
    _seed_menu_if_missing()
    print(f'[launcher] menu file        → {MENU_PATH}')
    print(f'[launcher] sonic interpreter → {_sonic_python()}')
    print(f'[launcher] voice session log → {VOICE_LOG_PATH}')
    if not os.path.isfile(TEST_HARNESS_SCRIPT):
        print(f'[launcher] WARNING: test_harness.py not found — check path above')
    server = HTTPServer(('0.0.0.0', PORT), Handler)
    print(f'[launcher] listening on  http://0.0.0.0:{PORT}')
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('[launcher] shutting down')
