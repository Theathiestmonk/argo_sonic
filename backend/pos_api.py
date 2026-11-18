#!/usr/bin/env python3
"""POS API – menu editing + the order ledger, one small service on port 8890.

MENU  – the single read/write path to the live menu file (src/argo_mini/menu/menu.json).
ORDERS – the POS order ledger (backend/orders_ledger.py, SQLite at src/argo_mini/orders/ledger.sqlite3):
  GET   /orders?status=active|processed|<status,…>&from=&to=&q=&table=&source=&payment=&limit=&offset=&sort=
  GET   /orders/<id> · /orders/summary?from=&to= · /orders/by-table · /orders/rev · /orders/export.csv?…filters
  POST  /orders                      create  {table, items:[{name, qty, price?, menu_id?, notes?}], source, notes, map}
  PATCH /orders/<id>                 {status, reason, items, notes, table, payment:{status,method}, version, actor}
  POST  /orders/clear-table          {table}   → serves every open order at that table

The dashboard's Menu page edits through this; the voice agent and the seeder read the same file, so an edit here
is what the robot sells and speaks on its next menu refresh.  (The launcher's own /menu goes through Postgres, which
is unavailable on this robot – this service does not depend on it.)

  GET  /menu            -> {"menu": [...], "settings": {...}, "savedAt": "..."}
  PUT  /menu            body {"menu": [...], "settings": {...}, "base": <savedAt you loaded>}
                        validated, written atomically (previous version kept as menu.json.bak); 409 if `base` is stale
  POST /menu/image?name=<dish>   raw image bytes -> cropped 16:10, 480x300 WebP, stored in frontend/{public,dist}/menu-images
  GET  /health

After a successful PUT the same document is pushed (best effort, in the background) to the launcher's POST /menu so
the database copy follows whenever the database is reachable.

Env: MENU_API_PORT (8890), MENU_FILE, ORDERS_DB, MENU_IMAGE_DIRS (os.pathsep-separated), LAUNCHER_URL.
"""
import io
import json
import os
import re
import sys
import tempfile
import threading
import time
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
from orders_ledger import Ledger, OrderError  # noqa: E402
MENU_FILE = Path(os.getenv("MENU_FILE", ROOT / "src/argo_mini/menu/menu.json"))
IMAGE_DIRS = [Path(p) for p in os.getenv(
    "MENU_IMAGE_DIRS", os.pathsep.join([str(ROOT / "frontend/public/menu-images"), str(ROOT / "frontend/dist/menu-images")])).split(os.pathsep)]
ORDERS_DB = Path(os.getenv("ORDERS_DB", ROOT / "src/argo_mini/orders/ledger.sqlite3"))
LAUNCHER_URL = os.getenv("LAUNCHER_URL", "http://127.0.0.1:8888")
PORT = int(os.getenv("MENU_API_PORT", "8890"))
MAX_IMAGE_BYTES = 10 * 1024 * 1024
IMG_W, IMG_H = 480, 300

_lock = threading.Lock()
ledger = None           # created in main() (tests may inject their own)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + f"{datetime.now(timezone.utc).microsecond // 1000:03d}Z"


def read_menu() -> dict:
    with open(MENU_FILE, encoding="utf-8") as f:
        d = json.load(f)
    d.setdefault("menu", [])
    d.setdefault("settings", {"currencyCode": "INR", "taxPercent": 0})
    d.setdefault("savedAt", None)
    return d


def _num(v, name, lo=0, hi=None, integer=False):
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise ValueError(f"{name} must be a number")
    if v < lo or (hi is not None and v > hi):
        raise ValueError(f"{name} must be between {lo} and {hi if hi is not None else '∞'}")
    return int(v) if integer else v


def validate(doc: dict) -> dict:
    """Returns a cleaned document or raises ValueError with a message the UI can show."""
    if not isinstance(doc, dict) or not isinstance(doc.get("menu"), list):
        raise ValueError("body must contain a 'menu' list")
    out, ids, names = [], set(), set()
    for n, it in enumerate(doc["menu"], 1):
        if not isinstance(it, dict):
            raise ValueError(f"item {n} is not an object")
        name = str(it.get("name", "")).strip()
        cat = str(it.get("category", "")).strip()
        if not name:
            raise ValueError(f"item {n} has no name")
        if not cat:
            raise ValueError(f"'{name}' needs a category")
        iid = str(it.get("id") or f"m-{int(time.time() * 1000)}-{n}")
        if iid in ids:
            raise ValueError(f"duplicate id {iid}")
        if name.lower() in names:
            raise ValueError(f"'{name}' appears twice – dish names must be unique")
        ids.add(iid); names.add(name.lower())
        diet = it.get("diet", "veg")
        if diet not in ("veg", "nonveg"):
            raise ValueError(f"'{name}': diet must be veg or nonveg")
        clean = {
            "id": iid, "name": name, "price": round(float(_num(it.get("price", 0), f"'{name}' price")), 2), "category": cat,
            "desc": str(it.get("desc", "")).strip(), "image": str(it.get("image", "")).strip(),
            "discountPercent": _num(it.get("discountPercent", 0), f"'{name}' discount", 0, 100, integer=True),
            "discountActive": bool(it.get("discountActive", False)), "eventLabel": str(it.get("eventLabel", "")).strip(),
            "available": bool(it.get("available", True)), "diet": diet,
        }
        if it.get("stock") not in (None, ""):
            clean["stock"] = _num(it["stock"], f"'{name}' stock", 0, integer=True)
        out.append(clean)
    s = doc.get("settings") or {}
    code = str(s.get("currencyCode", "INR")).strip().upper()
    if not re.fullmatch(r"[A-Z]{3}", code):
        raise ValueError("currency must be a 3-letter code")
    settings = {"currencyCode": code, "taxPercent": _num(s.get("taxPercent", 0), "tax", 0, 100)}
    return {"menu": out, "settings": settings}


def write_menu(doc: dict) -> dict:
    doc = dict(doc); doc["savedAt"] = _now()
    MENU_FILE.parent.mkdir(parents=True, exist_ok=True)
    if MENU_FILE.exists():
        (MENU_FILE.with_name(MENU_FILE.name + ".bak")).write_bytes(MENU_FILE.read_bytes())      # previous version
    fd, tmp = tempfile.mkstemp(dir=MENU_FILE.parent, prefix=".menu-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False, indent=2)
        try:
            os.chmod(tmp, (MENU_FILE.stat().st_mode & 0o777) if MENU_FILE.exists() else 0o664)    # keep the file's permissions
        except OSError:
            pass
        os.replace(tmp, MENU_FILE)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return doc


def _push_to_launcher(doc: dict) -> None:
    try:
        req = urllib.request.Request(f"{LAUNCHER_URL}/menu", data=json.dumps(doc).encode(), method="POST",
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=8).read()
    except Exception as e:                       # database down → file stays the source of truth
        print(f"[pos_api] launcher sync skipped: {e}", flush=True)


def save_image(raw: bytes, name: str) -> str:
    from PIL import Image, ImageOps
    img = ImageOps.exif_transpose(Image.open(io.BytesIO(raw))).convert("RGB")
    ar = IMG_W / IMG_H
    w, h = img.size
    if w / h > ar:
        nw = int(h * ar); img = img.crop(((w - nw) // 2, 0, (w - nw) // 2 + nw, h))
    else:
        nh = int(w / ar); img = img.crop((0, (h - nh) // 3, w, (h - nh) // 3 + nh))
    img = img.resize((IMG_W, IMG_H), Image.LANCZOS)
    fname = f"{re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-') or 'dish'}-{int(time.time())}.webp"
    for d in IMAGE_DIRS:
        d.mkdir(parents=True, exist_ok=True)
        img.save(d / fname, "WEBP", quality=74, method=6)
    return f"/menu-images/{fname}"


class Handler(BaseHTTPRequestHandler):
    server_version = "ArgoPosAPI/1.0"

    def log_message(self, fmt, *a):
        print(f"[pos_api] {self.address_string()} {fmt % a}", flush=True)

    def _send(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, PUT, POST, PATCH, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, code, text, ctype, filename=None):
        body = text.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Access-Control-Allow-Origin", "*")
        if filename:
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _orders(self, method, path, query):
        """All /orders routes. Raises OrderError for anything the caller got wrong."""
        f = {k: v[0] for k, v in query.items()}
        parts = [p for p in path.split("/") if p][1:]                       # after 'orders'
        if method == "GET":
            if not parts:
                return self._send(200, ledger.list(f))
            if parts == ["summary"]:
                return self._send(200, ledger.summary(f))
            if parts == ["by-table"]:
                return self._send(200, ledger.by_table())
            if parts == ["rev"]:
                return self._send(200, {"rev": ledger.rev()})
            if parts == ["export.csv"]:
                return self._send_text(200, ledger.export_csv(f), "text/csv; charset=utf-8", "orders.csv")
            if len(parts) == 1 and parts[0].isdigit():
                return self._send(200, ledger.get(int(parts[0])))
        body = json.loads(self._body(1024 * 1024) or b"{}") if method in ("POST", "PATCH") else {}
        actor = str(body.pop("actor", "") or self.headers.get("X-Actor", "") or "staff")[:30]
        if method == "POST" and not parts:
            return self._send(201, ledger.create(body, actor))
        if method == "POST" and parts == ["clear-table"]:
            return self._send(200, {"cleared": ledger.clear_table(str(body.get("table", "")), actor)})
        if method == "PATCH" and len(parts) == 1 and parts[0].isdigit():
            return self._send(200, ledger.patch(int(parts[0]), body, actor))
        self._send(404, {"error": "not found"})

    def _body(self, limit):
        n = int(self.headers.get("Content-Length", 0))
        if n > limit:
            raise ValueError(f"body too large (max {limit // 1024 // 1024} MB)")
        return self.rfile.read(n)

    def do_OPTIONS(self):
        self._send(204, {})

    def do_PATCH(self):
        u = urlparse(self.path)
        try:
            if u.path.startswith("/orders"):
                return self._orders("PATCH", u.path, parse_qs(u.query))
            self._send(404, {"error": "not found"})
        except OrderError as e:
            self._send(e.code, {"error": str(e), **({"current": ledger.get(int(u.path.split("/")[2]))} if e.code == 409 and u.path.split("/")[2:3] and u.path.split("/")[2].isdigit() else {})})
        except (ValueError, json.JSONDecodeError) as e:
            self._send(400, {"error": str(e)})

    def do_GET(self):
        u0 = urlparse(self.path)
        if u0.path.startswith("/orders"):
            try:
                return self._orders("GET", u0.path, parse_qs(u0.query))
            except OrderError as e:
                return self._send(e.code, {"error": str(e)})
            except ValueError as e:
                return self._send(400, {"error": str(e)})
        path = urlparse(self.path).path
        if path == "/health":
            return self._send(200, {"ok": True, "file": str(MENU_FILE)})
        if path == "/menu":
            try:
                with _lock:
                    return self._send(200, read_menu())
            except Exception as e:
                return self._send(500, {"error": f"cannot read menu: {e}"})
        self._send(404, {"error": "not found"})

    def do_PUT(self):
        if urlparse(self.path).path != "/menu":
            return self._send(404, {"error": "not found"})
        try:
            body = json.loads(self._body(5 * 1024 * 1024) or b"{}")
            clean = validate(body)
        except (ValueError, json.JSONDecodeError) as e:
            return self._send(400, {"error": str(e)})
        with _lock:
            try:
                cur = read_menu()
            except Exception:
                cur = {"savedAt": None}
            if body.get("base") is not None and cur.get("savedAt") and body["base"] != cur["savedAt"]:
                return self._send(409, {"error": "The menu was changed somewhere else – reloaded the latest.", "current": cur})
            saved = write_menu(clean)
        threading.Thread(target=_push_to_launcher, args=({"menu": saved["menu"], "settings": saved["settings"], "savedAt": saved["savedAt"]},), daemon=True).start()
        print(f"[pos_api] saved {len(saved['menu'])} items", flush=True)
        self._send(200, saved)

    def do_POST(self):
        u = urlparse(self.path)
        if u.path.startswith("/orders"):
            try:
                return self._orders("POST", u.path, parse_qs(u.query))
            except OrderError as e:
                return self._send(e.code, {"error": str(e)})
            except (ValueError, json.JSONDecodeError) as e:
                return self._send(400, {"error": str(e)})
        if u.path != "/menu/image":
            return self._send(404, {"error": "not found"})
        try:
            raw = self._body(MAX_IMAGE_BYTES)
            name = (parse_qs(u.query).get("name") or ["dish"])[0]
            self._send(200, {"image": save_image(raw, name)})
        except ValueError as e:
            self._send(413, {"error": str(e)})
        except Exception as e:
            self._send(400, {"error": f"not a usable image: {e}"})


def main():
    global ledger
    ledger = Ledger(ORDERS_DB, menu_reader=read_menu)
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"[pos_api] http://0.0.0.0:{PORT}  menu={MENU_FILE}  orders={ORDERS_DB}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
