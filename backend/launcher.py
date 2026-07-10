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
    GET  /status  →  {"running": bool, "pid": int|null}
    POST /start   →  starts start_slam_explore.sh in a new session
    POST /stop    →  kills the entire process group cleanly
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

SCRIPT  = os.path.join(_ROOT, 'start_slam_explore.sh')
PORT    = 8888

# ── State ────────────────────────────────────────────────────────────────────

_proc: subprocess.Popen | None = None
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
            self._json({'running': running, 'pid': pid})
        else:
            self._json({'error': 'not found'}, 404)

    def do_POST(self):
        global _proc

        if self.path == '/start':
            with _lock:
                if _proc and _proc.poll() is None:
                    self._json({'ok': True, 'status': 'already_running', 'pid': _proc.pid})
                    return
                if not os.path.isfile(SCRIPT):
                    self._json({'ok': False, 'error': f'script not found: {SCRIPT}'}, 500)
                    return
                _proc = subprocess.Popen(
                    ['bash', SCRIPT],
                    cwd=_ROOT,
                    start_new_session=True,   # own process group → clean kill
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            print(f'[launcher] stack started  pid={_proc.pid}')
            self._json({'ok': True, 'status': 'started', 'pid': _proc.pid})

        elif self.path == '/stop':
            with _lock:
                if _proc and _proc.poll() is None:
                    try:
                        os.killpg(os.getpgid(_proc.pid), signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                    _proc = None
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
    print(f'[launcher] script path → {SCRIPT}')
    if not os.path.isfile(SCRIPT):
        print(f'[launcher] WARNING: script not found — check path above')
    server = HTTPServer(('0.0.0.0', PORT), Handler)
    print(f'[launcher] listening on  http://0.0.0.0:{PORT}')
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('[launcher] shutting down')
