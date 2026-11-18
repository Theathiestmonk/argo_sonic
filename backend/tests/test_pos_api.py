import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

import pos_api
from orders_ledger import Ledger

MENU = {"settings": {"currencyCode": "INR", "taxPercent": 5}, "menu": [{"id": "m1", "name": "Butter Chicken", "price": 349}, {"id": "m3", "name": "Butter Naan", "price": 55}]}


@pytest.fixture
def api(tmp_path):
    pos_api.ledger = Ledger(tmp_path / "o.db", menu_reader=lambda: MENU)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), pos_api.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"

    def call(method, path, body=None):
        req = urllib.request.Request(base + path, method=method, data=json.dumps(body).encode() if body is not None else None, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                raw = r.read()
                return r.status, (json.loads(raw) if r.headers.get("Content-Type", "").startswith("application/json") else raw.decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())
    yield call
    srv.shutdown()


def test_order_lifecycle_over_http(api):
    s, o = api("POST", "/orders", {"table": "7", "items": [{"name": "Butter Chicken", "qty": 2}, {"name": "Water", "qty": 1}], "source": "voice_assistant", "actor": "voice"})
    assert s == 201 and o["number"] == 1001 and o["total"] == 732.9 and o["events"][0]["actor"] == "voice"
    oid = o["id"]
    assert api("GET", "/orders/rev")[1]["rev"] >= 1
    s, r = api("GET", "/orders?status=active"); assert s == 200 and r["total"] == 1 and r["counts"] == {"placed": 1}
    s, bt = api("GET", "/orders/by-table"); assert bt["7"]["total"] == 732.9 and bt["7"]["ids"] == [oid]
    s, o2 = api("PATCH", f"/orders/{oid}", {"status": "confirmed", "version": o["version"]}); assert s == 200 and o2["status"] == "confirmed"
    s, e = api("PATCH", f"/orders/{oid}", {"status": "ready", "version": o["version"]})                    # stale
    assert s == 409 and "another screen" in e["error"] and e["current"]["status"] == "confirmed"
    s, e = api("PATCH", f"/orders/{oid}", {"status": "cancelled"}); assert s == 400 and "reason" in e["error"]
    s, e = api("PATCH", f"/orders/{oid}", {"status": "delivered", "payment": {"status": "paid", "method": "upi"}})
    assert s == 200 and e["payment_status"] == "paid" and e["status"] == "delivered"
    s, sm = api("GET", "/orders/summary"); assert sm["orders"] == 1 and sm["paid"] == 732.9 and sm["open"] == 0
    s, csv_text = api("GET", "/orders/export.csv"); assert s == 200 and csv_text.startswith("Order,Placed") and "1001" in csv_text
    assert api("GET", "/orders/99999")[0] == 404
    assert api("POST", "/orders", {"table": "1", "items": []})[0] == 400
    s, c = api("POST", "/orders/clear-table", {"table": "7"}); assert c == {"cleared": 0}
