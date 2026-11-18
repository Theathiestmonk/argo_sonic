"""Voice-agent order sink ↔ the real POS API (backend/pos_api.py) running in-process on a temp database."""
import asyncio
import sys
import threading
from dataclasses import replace
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))
import pos_api                                   # noqa: E402
from orders_ledger import Ledger                 # noqa: E402

from ..config import load_settings
from ..robot.orders import FallbackSink, FileOrderSink, LedgerOrderSink, make_sink

MENU = {"settings": {"currencyCode": "INR", "taxPercent": 5}, "menu": [{"id": "m-in-026", "name": "Butter Chicken", "price": 349}]}
LINES = [{"name": "Butter Chicken", "kind": "menu", "qty": 2, "price": 349.0, "variation": "Spicy", "menu_id": "m-in-026"},
         {"name": "Water", "kind": "service", "qty": 1, "price": 0, "variation": None, "menu_id": None}]


def test_voice_order_lands_in_the_ledger(tmp_path):
    pos_api.ledger = Ledger(tmp_path / "o.db", menu_reader=lambda: MENU)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), pos_api.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        s = replace(load_settings(), pos_api_url=f"http://127.0.0.1:{srv.server_address[1]}", orders_dir=tmp_path, order_sink="auto", database_url="")
        ref = asyncio.run(make_sink(s, lambda: "Atsn_cafe_map").submit(3, LINES))
        assert ref == "ledger:#1001"
        o = pos_api.ledger.get(1001)
        assert (o["table_no"], o["source"], o["status"], o["map"]) == ("3", "voice_assistant", "placed", "Atsn_cafe_map")
        assert o["events"][0]["actor"] == "voice" and o["total"] == 732.9
        assert [(i["name"], i["qty"], i["notes"]) for i in o["items"]] == [("Butter Chicken", 2, "Spicy"), ("Water", 1, "")]
    finally:
        srv.shutdown()


def test_order_is_not_lost_when_the_api_is_down(tmp_path):
    s = replace(load_settings(), pos_api_url="http://127.0.0.1:9", orders_dir=tmp_path, order_sink="auto", database_url="")
    sink = make_sink(s, lambda: "m")
    assert isinstance(sink, FallbackSink) and isinstance(sink.sinks[0], LedgerOrderSink) and isinstance(sink.sinks[-1], FileOrderSink)
    assert asyncio.run(sink.submit(5, LINES)) == "file:5"
    assert (tmp_path / "m.json").exists()
