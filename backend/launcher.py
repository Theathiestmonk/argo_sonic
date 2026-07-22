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
    GET  /status  →  {"running": bool, "pid": int|null, "mode": str|null}
    POST /start   →  body {"mode": "manual"|"auto"} (default "auto")
                      "manual" → sh/start_slam_ui.sh          (SLAM only, no Nav2)
                      "auto"   → sh/start_slam_explore_ui.sh  (SLAM + Nav2 + frontier explorer)
    POST /stop    →  kills the entire process group cleanly

These *_ui.sh scripts are dedicated copies of the hand-run sh/start_slam.sh
and sh/start_slam_explore.sh — kept separate so UI-driven launches never
disturb the scripts used directly over SSH.
"""

import json
import os
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
PORT    = 8888

# ── State ────────────────────────────────────────────────────────────────────

_proc: subprocess.Popen | None = None
_mode: str | None = None
_lock = threading.Lock()


# ── HTTP handler ─────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):

    def do_OPTIONS(self):
        self._cors(200)

    def do_GET(self):
        if self.path == '/status':
            with _lock:
                running = _proc is not None and _proc.poll() is None
                pid     = _proc.pid if running else None
                mode    = _mode if running else None
            self._json({'running': running, 'pid': pid, 'mode': mode})
        else:
            self._json({'error': 'not found'}, 404)

    def do_POST(self):
        global _proc, _mode

        if self.path == '/start':
            mode = 'auto'
            try:
                length = int(self.headers.get('Content-Length', 0))
                if length:
                    body = json.loads(self.rfile.read(length))
                    if body.get('mode') == 'manual':
                        mode = 'manual'
            except (ValueError, json.JSONDecodeError):
                pass

            script = SLAM_SCRIPT if mode == 'manual' else EXPLORE_SCRIPT

            with _lock:
                if _proc and _proc.poll() is None:
                    self._json({'ok': True, 'status': 'already_running', 'pid': _proc.pid, 'mode': _mode})
                    return
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
                    ['bash', script, '--no-rviz'],
                    cwd=_ROOT,
                    start_new_session=True,   # own process group → clean kill
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                _mode = mode
            print(f'[launcher] stack started  mode={mode}  pid={_proc.pid}')
            self._json({'ok': True, 'status': 'started', 'pid': _proc.pid, 'mode': mode})

        elif self.path == '/stop':
            with _lock:
                if _proc and _proc.poll() is None:
                    try:
                        os.killpg(os.getpgid(_proc.pid), signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                    _proc = None
                _mode = None
            print('[launcher] stack stopped')
            self._json({'ok': True, 'status': 'stopped'})

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
    print(f'[launcher] manual script → {SLAM_SCRIPT}')
    print(f'[launcher] auto script   → {EXPLORE_SCRIPT}')
    if not os.path.isfile(SLAM_SCRIPT):
        print(f'[launcher] WARNING: manual script not found — check path above')
    if not os.path.isfile(EXPLORE_SCRIPT):
        print(f'[launcher] WARNING: auto script not found — check path above')
    server = HTTPServer(('0.0.0.0', PORT), Handler)
    print(f'[launcher] listening on  http://0.0.0.0:{PORT}')
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('[launcher] shutting down')
