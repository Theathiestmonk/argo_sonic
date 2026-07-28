"""
test_harness.py
----------------
Test/production driver for a single table visit, standing in for a real
Nav2 arrival trigger (which doesn't exist anywhere in this codebase yet —
see DEPLOYMENT.md / DashboardHome.jsx's own fixed-setTimeout "Arrived"
status for the same precedent). Simulates "robot arrived at this table"
after a fixed wait, then does one of four things depending on --action.
This is also what backend/launcher.py's POST /voice/start spawns as a
subprocess when a table action button is clicked in the UI — see that
endpoint's docstring for the process-lifecycle side of this.

Run:
    python test_harness.py --table 2 --map office_map --action order
    python test_harness.py --table 2 --map office_map --action deliver
    python test_harness.py --table 2 --map office_map --action bill
    python test_harness.py --table 2 --map office_map --action room_service
    python test_harness.py --table 2 --map office_map --wait 0   # skip the wait, iterate fast
"""

import argparse
import random
import sys
import time

import requests

import config
from conversation import ConversationContext
from main import keyboard_loop, print_banner, startup_checks, voice_loop_order_session
from tts import speak


def _table_display_name(map_name: str, table_id: str) -> str:
    """Best-effort — falls back to a generic label if the backend or that
    waypoint entry isn't reachable, since this is only used for the spoken
    arrival line, not for anything that needs to be correct to function."""
    try:
        resp = requests.get(f"{config.LAUNCHER_URL}/waypoints/{map_name}", timeout=3)
        resp.raise_for_status()
        entry = resp.json().get(table_id) or {}
        return entry.get("name") or f"Table {table_id}"
    except requests.RequestException:
        return f"Table {table_id}"


def _fetch_order(map_name: str, table_id: str) -> dict:
    """Best-effort fetch of this table's saved order — {} (not an
    exception) on any failure, same defensive style as _table_display_name
    above. Used by deliver/bill, which only announce what's already there —
    they don't take a new order (that's the 'order' action's job)."""
    try:
        resp = requests.get(f"{config.LAUNCHER_URL}/orders/{map_name}/{table_id}", timeout=3)
        resp.raise_for_status()
        return resp.json() or {}
    except requests.RequestException:
        return {}


def _order_summary(order: dict) -> str:
    items = order.get("items") or []
    return ", ".join(f"{it.get('qty', 1)} {it.get('name', 'item')}" for it in items)


# Beyond this many distinct line items, reciting every single one aloud
# stops being useful and just makes the handoff drag on — summarize by
# count + total instead. Distinct items, not total quantity: "5 Cokes" is
# still one quick line to say either way.
_DELIVER_ITEM_LIST_LIMIT = 3


# Price has no business being spoken during a delivery hand-off — that's
# what the 'bill' action is for. Picking a random line each time (instead of
# always the exact same sentence) is what makes this read as a greeting
# rather than a script being read aloud.
_DELIVER_ITEM_LINES = [
    "Here you go — {items}. Enjoy your meal!",
    "Your order has arrived — {items}. Hope you enjoy it!",
    "Order's here! {items}.",
    "Here's your food — {items}. Enjoy!",
]
_DELIVER_SUMMARY_LINES = [
    "Here you go — your order of {n} items! Enjoy your meal!",
    "Your order has arrived — {n} items, all ready for you. Enjoy!",
    "Order's here! {n} items, hope you enjoy them all!",
]


def _run_deliver(table_name: str, map_name: str, table_id: str):
    """v1 — one-shot fetch -> announce -> exit. No confirmation dialogue:
    the order was already finalized earlier by the 'order' action; this
    trip is just handing it off."""
    order = _fetch_order(map_name, table_id)
    items = order.get("items") or []
    if not items:
        speak(f"Arrived at {table_name}, but I don't see a saved order for this table.")
        return
    speak(f"Arrived at {table_name} with your order.")
    if len(items) > _DELIVER_ITEM_LIST_LIMIT:
        total_qty = sum(it.get("qty", 1) for it in items)
        speak(random.choice(_DELIVER_SUMMARY_LINES).format(n=total_qty))
    else:
        speak(random.choice(_DELIVER_ITEM_LINES).format(items=_order_summary(order)))


def _run_bill(table_name: str, map_name: str, table_id: str):
    """v1 — one-shot; reuses commands.py's REQUEST_BILL wording verbatim
    where it lines up. Does not wait for a spoken payment-method answer —
    that would need the same voice/keyboard loop as 'order', deliberately
    out of scope for this pass."""
    order = _fetch_order(map_name, table_id)
    if not order.get("items"):
        speak(f"Arrived at {table_name}. I don't see a saved order to bill yet.")
        return
    speak(f"Arrived at {table_name}.")
    speak("I'll get your bill ready right now.")
    speak(f"Your total comes to {order.get('total', 0):.2f} — that's {_order_summary(order)}.")
    speak("Will you be paying by card or cash?")


def _run_room_service(table_name: str):
    """v1 — no backend/order data exists for room service at all yet (it's
    a bare UI label today, see DashboardHome.jsx's TASKS). Adapted from
    commands.py's ROOM_DELIVERY hotel intent, substituting table for room
    since this harness is restaurant-scoped."""
    speak(f"Hi, I'm here at {table_name} for your room service request.")
    speak("Anything else you need?")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--table", required=True, help="Table id — same key used in waypoints/<map>.json")
    parser.add_argument("--map", required=True, dest="map_name", help="Map name — same as waypoints/<map>.json's filename")
    parser.add_argument("--wait", type=float, default=15.0, help="Seconds to simulate travel time before 'arriving' (default 15)")
    parser.add_argument("--action", choices=["order", "deliver", "bill", "room_service"], default="order",
                         help="What to do once 'arrived' (default: order)")
    args = parser.parse_args()

    print_banner()
    mode, ww_model = startup_checks()

    table_name = _table_display_name(args.map_name, args.table)

    print(f"  [Harness] Simulating travel to {table_name} (table={args.table}, map={args.map_name}, action={args.action})...")
    if args.wait > 0:
        time.sleep(args.wait)
    print(f"  [Harness] Arrived at {table_name}.")

    if args.action == "deliver":
        _run_deliver(table_name, args.map_name, args.table)
        return
    if args.action == "bill":
        _run_bill(table_name, args.map_name, args.table)
        return
    if args.action == "room_service":
        _run_room_service(table_name)
        return

    # action == "order" — unchanged from before --action existed
    context = ConversationContext()
    context.table_id = args.table
    context.map_name = args.map_name
    context.active_scenario = "restaurant"
    active_scenario = "restaurant"  # this harness is order-flow-scoped — skip scenario selection

    # The harness's own arrival line already asks "are you ready to order?" —
    # a real customer answers that directly (e.g. "two burgers"), they don't
    # repeat a trigger phrase back. So order-taking starts immediately here,
    # instead of waiting for a TAKE_ORDER fast-match that will never come.
    context.taking_order = True

    speak(f"Arrived at {table_name}. Are you ready to order?")

    if mode == "voice":
        # NOT voice_loop() — that always waits for "Hey Sonic" first, but
        # Sonic just spoke to the customer directly above; see
        # voice_loop_order_session's docstring for why that distinction
        # matters (it's the reason nothing happened after the arrival line
        # got spoken, the first time this was tried against real hardware).
        voice_loop_order_session(context, active_scenario)
    else:
        keyboard_loop(context, active_scenario)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n  Sonic: Goodbye!\n")
        sys.exit(0)
