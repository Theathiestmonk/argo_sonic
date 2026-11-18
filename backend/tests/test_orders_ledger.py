import pytest

from orders_ledger import Ledger, OrderError

MENU = {"settings": {"currencyCode": "INR", "taxPercent": 5},
        "menu": [{"id": "m1", "name": "Butter Chicken", "price": 349, "discountActive": False},
                 {"id": "m2", "name": "Mango Lassi", "price": 120, "discountActive": True, "discountPercent": 15},
                 {"id": "m3", "name": "Butter Naan", "price": 55}]}


@pytest.fixture
def L(tmp_path):
    state = {"menu": MENU}
    led = Ledger(tmp_path / "t.db", menu_reader=lambda: state["menu"])
    led.state = state
    return led


def mk(L, table="3", **kw):
    return L.create({"table": table, "items": [{"name": "Butter Chicken", "qty": 2}, {"name": "Mango Lassi", "qty": 1}], **kw}, actor="test")


def test_numbering_pricing_and_tax(L):
    o = mk(L)
    assert o["number"] == 1001 and mk(L)["number"] == 1002
    assert o["status"] == "placed" and o["payment_status"] == "unpaid"
    lassi = [i for i in o["items"] if i["name"] == "Mango Lassi"][0]
    assert lassi["list_price"] == 120 and lassi["unit_price"] == 102.0       # 15% offer snapshot
    assert o["subtotal"] == 2 * 349 + 102 == 800.0
    assert o["tax_percent"] == 5 and o["tax"] == 40.0 and o["total"] == 840.0
    assert [e["kind"] for e in o["events"]] == ["created"]


def test_snapshot_survives_menu_changes(L):
    o = mk(L)
    L.state["menu"] = {"settings": {"currencyCode": "INR", "taxPercent": 18}, "menu": [{"id": "m1", "name": "Butter Chicken", "price": 999}]}
    again = L.get(o["id"])
    assert again["total"] == 840.0 and again["items"][0]["unit_price"] == 349


def test_unknown_item_is_a_free_extra_and_validation(L):
    o = L.create({"table": "1", "items": [{"name": "Water", "qty": 1}, {"name": "Butter Naan", "qty": 3, "variation": "extra butter"}]})
    w = [i for i in o["items"] if i["name"] == "Water"][0]
    assert w["unit_price"] == 0 and w["kind"] == "service"
    assert o["total"] == round(165 * 1.05, 2)
    for bad in ([], [{"name": "", "qty": 1}], [{"name": "X", "qty": 0}], [{"name": "X", "qty": 100}], [{"name": "X", "qty": 1, "price": -1}]):
        with pytest.raises(OrderError):
            L.create({"table": "1", "items": bad})
    with pytest.raises(OrderError):
        L.create({"table": "1", "items": [{"name": "X", "qty": 1}], "source": "carrier-pigeon"})


def test_status_lifecycle_rules(L):
    o = mk(L); oid = o["id"]
    for to in ("confirmed", "preparing", "ready"):
        o = L.patch(oid, {"status": to})
        assert o["status"] == to
    with pytest.raises(OrderError) as e:
        L.patch(oid, {"status": "placed"}); assert e.value.code == 409           # not two steps back
    assert L.patch(oid, {"status": "preparing"})["status"] == "preparing"        # one step back is allowed
    o = L.patch(oid, {"status": "delivered"})                                    # skipping ahead is allowed
    assert o["status"] == "delivered"
    with pytest.raises(OrderError):
        L.patch(oid, {"status": "cancelled", "reason": "x"})                     # closed orders stay closed
    kinds = [(e["from_status"], e["to_status"]) for e in o["events"] if e["kind"] == "status"]
    assert kinds[0] == ("placed", "confirmed") and kinds[-1] == ("preparing", "delivered")
    assert L.patch(oid, {"status": "refunded"})["status"] == "refunded"


def test_cancel_needs_a_reason_and_is_kept(L):
    o = mk(L)
    with pytest.raises(OrderError):
        L.patch(o["id"], {"status": "cancelled"})
    c = L.patch(o["id"], {"status": "cancelled", "reason": "Guest left"})
    assert c["status"] == "cancelled" and c["cancel_reason"] == "Guest left"
    assert L.get(o["id"])["id"] == o["id"]                                       # never deleted
    assert L.summary({})["cancelled"] == 1 and L.summary({})["orders"] == 0       # not counted as sales


def test_items_editable_only_before_kitchen(L):
    o = mk(L); oid = o["id"]
    o = L.patch(oid, {"items": [{"name": "Butter Naan", "qty": 4}]})
    assert o["subtotal"] == 220 and o["total"] == 231.0 and len(o["items"]) == 1
    L.patch(oid, {"status": "confirmed"}); L.patch(oid, {"items": [{"name": "Butter Naan", "qty": 2}]})
    L.patch(oid, {"status": "preparing"})
    with pytest.raises(OrderError) as e:
        L.patch(oid, {"items": [{"name": "Butter Naan", "qty": 1}]}); assert e.value.code == 409


def test_payment_rules(L):
    o = mk(L); oid = o["id"]
    with pytest.raises(OrderError):
        L.patch(oid, {"payment": {"status": "paid"}})                            # method required
    with pytest.raises(OrderError):
        L.patch(oid, {"payment": {"status": "refunded"}})                        # only paid orders refund
    p = L.patch(oid, {"payment": {"status": "paid", "method": "upi"}})
    assert p["payment_status"] == "paid" and p["payment_method"] == "upi" and p["paid_at"]
    assert L.patch(oid, {"payment": {"status": "refunded"}})["payment_status"] == "refunded"
    c = mk(L); L.patch(c["id"], {"status": "cancelled", "reason": "oops"})
    with pytest.raises(OrderError):
        L.patch(c["id"], {"payment": {"status": "paid", "method": "cash"}})


def test_optimistic_concurrency(L):
    o = mk(L)
    L.patch(o["id"], {"status": "confirmed", "version": o["version"]})
    with pytest.raises(OrderError) as e:
        L.patch(o["id"], {"status": "preparing", "version": o["version"]})        # stale screen
    assert e.value.code == 409


def test_list_filters_counts_search_and_rev(L):
    a = mk(L, "1"); b = mk(L, "2"); c = L.create({"table": "3", "items": [{"name": "Butter Naan", "qty": 1}], "source": "voice_assistant", "notes": "no butter please"})
    L.patch(b["id"], {"status": "preparing"}); L.patch(c["id"], {"status": "cancelled", "reason": "dup"})
    r0 = L.rev()
    r = L.list({"status": "active"}); assert {o["id"] for o in r["orders"]} == {a["id"], b["id"]}
    assert r["counts"] == {"placed": 1, "preparing": 1, "cancelled": 1}
    assert [o["id"] for o in L.list({"status": "processed"})["orders"]] == [c["id"]]
    assert [o["id"] for o in L.list({"q": "lassi"})["orders"]] == [b["id"], a["id"]]
    assert [o["id"] for o in L.list({"q": "#%d" % a["id"]})["orders"]] == [a["id"]]
    assert [o["id"] for o in L.list({"q": "no butter"})["orders"]] == [c["id"]]
    assert [o["id"] for o in L.list({"source": "voice_assistant"})["orders"]] == [c["id"]]
    assert L.list({"limit": 1, "offset": 1})["orders"][0]["id"] == b["id"] and L.list({})["total"] == 3
    assert L.list({"from": "2999-01-01T00:00:00.000Z"})["total"] == 0
    assert L.rev() == r0


def test_summary_by_table_clear_and_csv(L):
    a = mk(L, "4"); b = L.create({"table": "4", "items": [{"name": "Butter Naan", "qty": 1}]}); mk(L, "5")
    L.patch(a["id"], {"status": "ready"}); L.patch(a["id"], {"payment": {"status": "paid", "method": "cash"}})
    s = L.summary({})
    assert s["orders"] == 3 and s["open"] == 3 and s["paid"] == 840.0 and s["sales"] == round(840 + 57.75 + 840, 2)
    t = L.by_table()
    assert set(t) == {"4", "5"} and t["4"]["total"] == round(840 + 57.75, 2) and t["4"]["status"] == "placed" and t["4"]["ids"] == [a["id"], b["id"]]
    assert len(t["4"]["items"]) == 3
    assert L.clear_table("4") == 2 and set(L.by_table()) == {"5"}
    assert L.get(a["id"])["status"] == "delivered"
    rows = L.export_csv({}).strip().splitlines()
    assert len(rows) == 4 and rows[0].startswith("Order,Placed")
