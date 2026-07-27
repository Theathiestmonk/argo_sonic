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

These *_ui.sh scripts are dedicated copies of the hand-run sh/start_slam.sh
and sh/start_slam_explore.sh — kept separate so UI-driven launches never
disturb the scripts used directly over SSH.
"""

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
PORT    = 8888

_NAME_RE = re.compile(r'^[A-Za-z0-9_-]+$')

def _safe_name(name):
    """Reject anything but a bare map/waypoint-set name — these come straight
    from the URL and get joined into filesystem paths, so this is the only
    thing standing between a stray '..' and reading/writing outside MAPS_DIR
    / WAYPOINTS_DIR."""
    return name if _NAME_RE.match(name or '') else None

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

        else:
            self._json({'error': 'not found'}, 404)

    # ── helpers ──────────────────────────────────────────────────────────────

    def _json(self, data: dict, code: int = 200):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header('Content-Type',  'application/json')
        self.send_header('Access-Control-Allow-Origin',  '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _cors(self, code: int):
        self.send_response(code)
        self.send_header('Access-Control-Allow-Origin',  '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
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
    server = HTTPServer(('0.0.0.0', PORT), Handler)
    print(f'[launcher] listening on  http://0.0.0.0:{PORT}')
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('[launcher] shutting down')
