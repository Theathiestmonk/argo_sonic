"""
test_voice_agent.py — Full action-flow integration tests for Sonic's
main_agent.py voice agent. Exercises every graph node and major path through
the LangGraph StateGraph without real hardware, network, or database calls.

All external dependencies are mocked:
  - LLM (render/extract_slots) -> deterministic responses
  - STT/TTS (Sarvam)           -> bypassed (text mode)
  - Database (Postgres)         -> in-memory stubs
  - Navigation (nav_bridge.py)  -> instant success/failure
  - Audio (sounddevice)         -> not loaded
  - Wake word (openwakeword)    -> not loaded

Run with:
    cd /home/shivu/argo_sonic
    .venv/bin/python -m pytest test_voice_agent.py -v
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from dataclasses import asdict
from unittest.mock import MagicMock, patch

# Set env vars BEFORE importing main_agent so its module-level config picks
# them up. No real API keys needed — we mock every outbound call.
os.environ.setdefault("OPENAI_API_KEY", "sk-test-fake-key-for-testing")
os.environ.setdefault("SARVAM_API_KEY", "sk_test_fake_sarvam_key")
os.environ.setdefault("DATABASE_URL", "")
os.environ.setdefault("SONIC_SKIP_NAV", "1")
os.environ.setdefault("SONIC_TRACE", "0")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "sonic"))

from sonic.main_agent import (
    DISPATCH_TAKE_ORDER_ACTIONS,
    TIMEOUT_SENTINEL,
    LLMClient,
    OrderState,
    build_graph,
    clean_spoken_text,
    commit_and_continue,
    find_menu_category,
    find_menu_item,
    find_order_line,
    get_item_price,
    menu_categories,
    menu_items_by_category,
    menu_items_summary,
    order_summary,
    route_active_item,
    table_number_for_db,
    table_ref_for_nav,
    _safe_int,
)
import sonic.main_agent as ma


# ---------------------------------------------------------------------------
# Test menu data — loaded once into the module globals before any test runs.
# Mirrors the shape load_menu_from_db() produces.
# ---------------------------------------------------------------------------

FAKE_MENU = [
    {"item_id": "1", "name": "Margherita Pizza", "category": "main_course",
     "price": 350.0, "available": True, "aliases": ["margherita", "cheese pizza"], "description": "Classic cheese pizza"},
    {"item_id": "2", "name": "Masala Dosa", "category": "main_course",
     "price": 150.0, "available": True, "aliases": ["dosa"], "description": "Crispy dosa with masala filling"},
    {"item_id": "3", "name": "Paneer Tikka", "category": "starters",
     "price": 250.0, "available": True, "aliases": ["tikka"], "description": "Grilled paneer cubes"},
    {"item_id": "4", "name": "Gulab Jamun", "category": "desserts",
     "price": 120.0, "available": True, "aliases": ["gulab jamun"], "description": "Sweet milk dumplings"},
    {"item_id": "5", "name": "Fresh Lime Soda", "category": "beverages",
     "price": 80.0, "available": True, "aliases": ["lime soda", "nimbu soda"], "description": "Fresh lime with soda"},
    {"item_id": "6", "name": "Butter Naan", "category": "breads",
     "price": 60.0, "available": True, "aliases": ["naan"], "description": "Soft butter naan"},
    {"item_id": "7", "name": "Tomato Soup", "category": "soups",
     "price": 100.0, "available": True, "aliases": ["soup"], "description": "Classic tomato soup"},
    {"item_id": "8", "name": "Caesar Salad", "category": "salads",
     "price": 180.0, "available": True, "aliases": ["salad"], "description": "Fresh Caesar salad"},
    {"item_id": "9", "name": "French Fries", "category": "snacks",
     "price": 120.0, "available": True, "aliases": ["fries"], "description": "Crispy french fries"},
    {"item_id": "10", "name": "Mango Lassi", "category": "drinks",
     "price": 90.0, "available": True, "aliases": ["lassi"], "description": "Sweet mango lassi"},
]


def _install_fake_menu():
    ma.MENU_ITEMS = list(FAKE_MENU)
    lookup = {}
    for item in FAKE_MENU:
        lookup[item["name"].strip().lower()] = item
        for alias in item["aliases"]:
            lookup[alias.strip().lower()] = item
    ma.MENU_LOOKUP = lookup


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_state(**overrides) -> OrderState:
    s = OrderState()
    for k, v in overrides.items():
        setattr(s, k, v)
    return s


def fake_extract(intent=None, table_no=None, items=None, quantity=None,
                 preference=None, yes_no=None, wants_more=None,
                 cancel_or_change=None, edit_target=None, edit_action=None,
                 edit_value=None, category=None, answers_pending=True,
                 response_text=""):
    return {
        "intent": intent,
        "table_no": table_no,
        "answers_pending": answers_pending,
        "items": items or [],
        "quantity": quantity,
        "preference": preference,
        "yes_no": yes_no,
        "wants_more": wants_more,
        "cancel_or_change": cancel_or_change,
        "edit_target": edit_target,
        "edit_action": edit_action,
        "edit_value": edit_value,
        "category": category,
        "response_text": response_text,
    }


# ---------------------------------------------------------------------------
# Unit tests for pure helper functions
# ---------------------------------------------------------------------------

class TestHelperFunctions(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        _install_fake_menu()

    def test_table_ref_for_nav_bare_digit(self):
        self.assertEqual(table_ref_for_nav("3"), "Table 3")

    def test_table_ref_for_nav_already_named(self):
        self.assertEqual(table_ref_for_nav("Table 3"), "Table 3")

    def test_table_number_for_db_full_name(self):
        self.assertEqual(table_number_for_db("Table 5"), "5")

    def test_table_number_for_db_bare(self):
        self.assertEqual(table_number_for_db("3"), "3")

    def test_order_summary_empty(self):
        s = make_state()
        self.assertEqual(order_summary(s), "no items yet")

    def test_order_summary_with_items(self):
        s = make_state(order_items=[
            {"name": "Margherita Pizza", "quantity": 2, "preference": "extra cheese"},
            {"name": "Fresh Lime Soda", "quantity": 1},
        ])
        summary = order_summary(s)
        self.assertIn("2x Margherita Pizza (extra cheese)", summary)
        self.assertIn("1x Fresh Lime Soda", summary)

    def test_clean_spoken_text_strips_quotes(self):
        self.assertEqual(clean_spoken_text('"Hello there!"'), "Hello there!")
        self.assertEqual(clean_spoken_text("'Hi!'"), "Hi!")

    def test_clean_spoken_text_capitalizes(self):
        self.assertEqual(clean_spoken_text("hello"), "Hello")

    def test_clean_spoken_text_empty(self):
        self.assertEqual(clean_spoken_text(""), "")
        self.assertEqual(clean_spoken_text(None), "")

    def test_safe_int_valid(self):
        self.assertEqual(_safe_int(3), 3)
        self.assertEqual(_safe_int("5"), 5)

    def test_safe_int_invalid(self):
        self.assertIsNone(_safe_int(None))
        self.assertIsNone(_safe_int("abc"))
        self.assertIsNone(_safe_int(0))
        self.assertIsNone(_safe_int(-1))

    def test_find_menu_item_exact(self):
        item = find_menu_item("Margherita Pizza")
        self.assertIsNotNone(item)
        self.assertEqual(item["item_id"], "1")

    def test_find_menu_item_alias(self):
        item = find_menu_item("dosa")
        self.assertIsNotNone(item)
        self.assertEqual(item["name"], "Masala Dosa")

    def test_find_menu_item_fuzzy(self):
        item = find_menu_item("margarita pizza")
        self.assertIsNotNone(item)
        self.assertEqual(item["item_id"], "1")

    def test_find_menu_item_none(self):
        self.assertIsNone(find_menu_item(None))
        self.assertIsNone(find_menu_item("nonexistent dish xyz"))

    def test_menu_categories(self):
        cats = menu_categories()
        self.assertIn("main_course", cats)
        self.assertIn("starters", cats)
        self.assertIn("desserts", cats)

    def test_menu_items_by_category(self):
        items = menu_items_by_category("starters")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["name"], "Paneer Tikka")

    def test_menu_items_summary(self):
        items = menu_items_by_category("starters")
        summary = menu_items_summary(items)
        self.assertIn("Paneer Tikka", summary)
        self.assertIn("₹250.00", summary)

    def test_find_menu_category_exact(self):
        self.assertEqual(find_menu_category("starters"), "starters")

    def test_find_menu_category_fuzzy(self):
        result = find_menu_category("starter")
        self.assertEqual(result, "starters")

    def test_find_menu_category_none(self):
        self.assertIsNone(find_menu_category(None))

    def test_get_item_price(self):
        self.assertEqual(get_item_price({"item_id": "1"}), 350.0)
        self.assertEqual(get_item_price({"item_id": "999"}), 0.0)

    def test_find_order_line(self):
        s = make_state(order_items=[
            {"item_id": "1", "name": "Margherita Pizza", "quantity": 2},
        ])
        line = find_order_line(s, "Margherita Pizza")
        self.assertIsNotNone(line)
        self.assertEqual(line["item_id"], "1")

    def test_find_order_line_not_found(self):
        s = make_state(order_items=[])
        self.assertIsNone(find_order_line(s, "Nonexistent"))


# ---------------------------------------------------------------------------
# Unit tests for OrderState routing helpers
# ---------------------------------------------------------------------------

class TestRoutingHelpers(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        _install_fake_menu()

    def test_route_active_item_needs_qty(self):
        s = make_state(active_item={
            "item_id": "1", "name": "Margherita Pizza", "category": "main_course",
            "quantity": None, "preference": None, "preference_asked": False,
        })
        self.assertEqual(route_active_item(s), "n_ask_qty")

    def test_route_active_item_needs_preference(self):
        s = make_state(active_item={
            "item_id": "1", "name": "Margherita Pizza", "category": "main_course",
            "quantity": 2, "preference": None, "preference_asked": False,
        })
        self.assertEqual(route_active_item(s), "n_ask_preference")

    def test_route_active_item_complete_no_pending(self):
        s = make_state(
            active_item={
                "item_id": "1", "name": "Margherita Pizza", "category": "main_course",
                "quantity": 2, "preference": "extra cheese", "preference_asked": True,
            },
            pending_items=[],
        )
        result = route_active_item(s)
        self.assertEqual(result, "n_ask_anything_else")
        self.assertEqual(len(s.order_items), 1)

    def test_route_active_item_complete_with_pending(self):
        pending = {"item_name": "Masala Dosa", "quantity": 1, "preference": None}
        s = make_state(
            active_item={
                "item_id": "1", "name": "Margherita Pizza", "category": "main_course",
                "quantity": 2, "preference": None, "preference_asked": True,
            },
            pending_items=[pending],
        )
        result = route_active_item(s)
        self.assertEqual(result, "n_extract_items")
        self.assertEqual(len(s.order_items), 1)

    def test_commit_and_continue_no_pending(self):
        s = make_state(
            active_item={"item_id": "1", "name": "Pizza", "quantity": 1},
            pending_items=[],
        )
        result = commit_and_continue(s)
        self.assertEqual(result, "n_ask_anything_else")
        self.assertIsNone(s.active_item)
        self.assertEqual(len(s.order_items), 1)

    def test_commit_and_continue_with_pending(self):
        s = make_state(
            active_item={"item_id": "1", "name": "Pizza", "quantity": 1},
            pending_items=[{"item_name": "Dosa"}],
        )
        result = commit_and_continue(s)
        self.assertEqual(result, "n_extract_items")


# ---------------------------------------------------------------------------
# Node-level tests (each node function called directly with mock state)
# ---------------------------------------------------------------------------

class TestNodeIntentClassify(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        _install_fake_menu()

    @patch.object(ma, "interpret")
    @patch.object(ma, "render", return_value="Hi! How can I help?")
    @patch("sonic.main_agent.interrupt")
    def test_voice_take_order_with_table(self, mock_interrupt, mock_render, mock_interpret):
        mock_interrupt.return_value = "I want to order food at table 3"
        def _interpret_side_effect(state, transcript, expects):
            state.table_no = "3"
            return fake_extract(
                intent="take_order", table_no="3",
                items=[{"item_name": "Margherita Pizza", "quantity": 2, "preference": None}],
            )
        mock_interpret.side_effect = _interpret_side_effect
        s = make_state()
        result = ma.n_intent_classify(s)
        self.assertEqual(s.next_node, "n_navigate_to_table")
        self.assertEqual(s.table_no, "3")
        self.assertIsNotNone(s.pending_seed)
        self.assertEqual(s.post_arrival_node, "n_greet_and_ask_order")

    @patch.object(ma, "interpret")
    @patch.object(ma, "render", return_value="Hi!")
    @patch("sonic.main_agent.interrupt")
    def test_voice_take_order_no_table(self, mock_interrupt, mock_render, mock_interpret):
        mock_interrupt.return_value = "I want to order"
        mock_interpret.return_value = fake_extract(intent="take_order")
        s = make_state()
        ma.n_intent_classify(s)
        self.assertEqual(s.next_node, "n_ask_table")

    @patch.object(ma, "interpret")
    @patch.object(ma, "render", return_value="Hi!")
    @patch("sonic.main_agent.interrupt")
    def test_voice_menu_intent(self, mock_interrupt, mock_render, mock_interpret):
        mock_interrupt.return_value = "Show me the menu"
        mock_interpret.return_value = fake_extract(intent="menu", category="starters")
        s = make_state()
        ma.n_intent_classify(s)
        self.assertEqual(s.next_node, "n_ask_table")
        self.assertEqual(s.post_arrival_node, "n_menu_pick_category")

    @patch.object(ma, "interpret")
    @patch.object(ma, "render", return_value="Hi!")
    @patch("sonic.main_agent.interrupt")
    def test_voice_stub_intent(self, mock_interrupt, mock_render, mock_interpret):
        mock_interrupt.return_value = "Get the bill"
        mock_interpret.return_value = fake_extract(intent="get_bill")
        s = make_state()
        ma.n_intent_classify(s)
        self.assertEqual(s.next_node, "n_stub")

    @patch.object(ma, "say")
    @patch.object(ma, "render", return_value="Hi!")
    @patch("sonic.main_agent.interrupt")
    def test_voice_timeout(self, mock_interrupt, mock_render, mock_say):
        mock_interrupt.return_value = TIMEOUT_SENTINEL
        s = make_state()
        ma.n_intent_classify(s)
        self.assertEqual(s.next_node, "n_respond")
        self.assertTrue(s.went_idle)

    def test_forced_take_order(self):
        orig_loc, orig_act = ma.FORCED_LOCATION, ma.FORCED_ACTION
        try:
            ma.FORCED_LOCATION = "Table 3"
            ma.FORCED_ACTION = "order"
            s = make_state(first_turn=True)
            ma.n_intent_classify(s)
            self.assertEqual(s.next_node, "n_navigate_to_table")
            self.assertEqual(s.table_no, "Table 3")
            self.assertEqual(s.trigger_source, "ui")
            self.assertEqual(s.post_arrival_node, "n_greet_and_ask_order")
        finally:
            ma.FORCED_LOCATION = orig_loc
            ma.FORCED_ACTION = orig_act

    def test_forced_deliver(self):
        orig_loc, orig_act = ma.FORCED_LOCATION, ma.FORCED_ACTION
        try:
            ma.FORCED_LOCATION = "Table 2"
            ma.FORCED_ACTION = "deliver"
            s = make_state(first_turn=True)
            ma.n_intent_classify(s)
            self.assertEqual(s.next_node, "n_navigate_to_table")
            self.assertEqual(s.post_arrival_node, "n_deliver_announce")
        finally:
            ma.FORCED_LOCATION = orig_loc
            ma.FORCED_ACTION = orig_act

    def test_forced_unsupported_action(self):
        orig_loc, orig_act = ma.FORCED_LOCATION, ma.FORCED_ACTION
        try:
            ma.FORCED_LOCATION = "Table 1"
            ma.FORCED_ACTION = "bill"
            s = make_state(first_turn=True)
            ma.n_intent_classify(s)
            self.assertEqual(s.next_node, "n_stub")
        finally:
            ma.FORCED_LOCATION = orig_loc
            ma.FORCED_ACTION = orig_act


class TestNodeAskTable(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        _install_fake_menu()

    @patch.object(ma, "get_reply_or_route")
    def test_table_found(self, mock_grr):
        mock_grr.return_value = fake_extract(table_no="3")
        s = make_state()
        s.table_no = "3"  # interpret() sets this on the state
        ma.n_ask_table(s)
        self.assertEqual(s.next_node, "n_navigate_to_table")

    @patch.object(ma, "get_reply_or_route")
    def test_table_not_found_retries(self, mock_grr):
        mock_grr.return_value = fake_extract()
        s = make_state()
        ma.n_ask_table(s)
        self.assertEqual(s.next_node, "n_ask_table")
        self.assertTrue(s.ask_retry)

    @patch.object(ma, "get_reply_or_route")
    def test_table_timeout(self, mock_grr):
        mock_grr.return_value = None
        s = make_state()
        s.next_node = "n_respond"
        ma.n_ask_table(s)
        self.assertEqual(s.next_node, "n_respond")


class TestNodeNavigateToTable(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        _install_fake_menu()

    @patch.object(ma, "db_start_session")
    @patch.object(ma, "report_phase")
    @patch.object(ma, "perform_travel_action", return_value=True)
    def test_nav_success(self, mock_travel, mock_phase, mock_db):
        s = make_state(table_no="3", post_arrival_node="n_greet_and_ask_order")
        ma.n_navigate_to_table(s)
        self.assertTrue(s.location_reached)
        self.assertEqual(s.next_node, "n_greet_and_ask_order")
        mock_db.assert_called_once()

    @patch.object(ma, "say")
    @patch.object(ma, "report_phase")
    @patch.object(ma, "perform_travel_action", return_value=False)
    def test_nav_failure(self, mock_travel, mock_phase, mock_say):
        s = make_state(table_no="3")
        ma.n_navigate_to_table(s)
        self.assertFalse(s.location_reached)
        self.assertEqual(s.next_node, "n_respond")
        self.assertTrue(s.went_idle)

    @patch.object(ma, "db_start_session")
    def test_already_at_table(self, mock_db):
        s = make_state(table_no="3", location_reached=True, post_arrival_node="n_greet_and_ask_order")
        ma.n_navigate_to_table(s)
        self.assertEqual(s.next_node, "n_greet_and_ask_order")
        mock_db.assert_not_called()


class TestNodeGreetAndAskOrder(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        _install_fake_menu()

    @patch.object(ma, "interpret")
    @patch.object(ma, "render", return_value="Hello! What would you like?")
    @patch("sonic.main_agent.interrupt")
    def test_guest_names_items(self, mock_interrupt, mock_render, mock_interpret):
        mock_interrupt.return_value = "Two margherita pizzas"
        mock_interpret.return_value = fake_extract(
            items=[{"item_name": "Margherita Pizza", "quantity": 2, "preference": None}],
        )
        s = make_state(table_no="3", location_reached=True)
        ma.n_greet_and_ask_order(s)
        self.assertEqual(s.next_node, "n_extract_items")
        self.assertIsNotNone(s.pending_seed)

    @patch.object(ma, "say")
    @patch.object(ma, "render", return_value="Hello!")
    @patch("sonic.main_agent.interrupt")
    def test_guest_timeout(self, mock_interrupt, mock_render, mock_say):
        mock_interrupt.return_value = TIMEOUT_SENTINEL
        s = make_state(table_no="3", location_reached=True)
        ma.n_greet_and_ask_order(s)
        self.assertEqual(s.next_node, "n_respond")
        self.assertTrue(s.went_idle)

    def test_pending_seed_fast_path(self):
        s = make_state(
            table_no="3", location_reached=True,
            pending_seed={"items": [{"item_name": "Margherita Pizza", "quantity": 2}]},
        )
        ma.n_greet_and_ask_order(s)
        self.assertEqual(s.next_node, "n_extract_items")


class TestNodeExtractItems(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        _install_fake_menu()

    def test_seed_with_valid_item_fully_specified(self):
        """When qty and preference are both given, the item is committed
        immediately (route_active_item -> commit_and_continue) and
        active_item becomes None."""
        s = make_state(
            table_no="3", location_reached=True,
            pending_seed={"items": [{"item_name": "Margherita Pizza", "quantity": 2, "preference": "extra cheese"}]},
        )
        ma.n_extract_items(s)
        self.assertIsNone(s.active_item)
        self.assertEqual(len(s.order_items), 1)
        self.assertEqual(s.order_items[0]["item_id"], "1")
        self.assertEqual(s.order_items[0]["quantity"], 2)
        self.assertEqual(s.order_items[0]["preference"], "extra cheese")
        self.assertEqual(s.next_node, "n_ask_anything_else")

    def test_seed_with_valid_item_needs_qty(self):
        """When only item name is given (no qty), active_item stays set
        and next_node is n_ask_qty."""
        s = make_state(
            table_no="3", location_reached=True,
            pending_seed={"items": [{"item_name": "Margherita Pizza", "quantity": None, "preference": None}]},
        )
        ma.n_extract_items(s)
        self.assertIsNotNone(s.active_item)
        self.assertEqual(s.active_item["item_id"], "1")
        self.assertEqual(s.next_node, "n_ask_qty")

    def test_seed_with_unknown_item(self):
        s = make_state(
            table_no="3", location_reached=True,
            pending_seed={"items": [{"item_name": "Nonexistent Dish XYZ", "quantity": 1, "preference": None}]},
        )
        ma.n_extract_items(s)
        self.assertEqual(s.next_node, "n_extract_items")
        self.assertEqual(s.item_attempts, 1)

    def test_seed_multiple_items_queues_rest(self):
        s = make_state(
            table_no="3", location_reached=True,
            pending_seed={"items": [
                {"item_name": "Margherita Pizza", "quantity": 2, "preference": None},
                {"item_name": "Fresh Lime Soda", "quantity": 1, "preference": None},
            ]},
        )
        ma.n_extract_items(s)
        self.assertIsNotNone(s.active_item)
        self.assertEqual(s.active_item["name"], "Margherita Pizza")
        self.assertEqual(len(s.pending_items), 1)

    def test_too_many_failed_attempts(self):
        with patch.object(ma, "say"):
            s = make_state(
                table_no="3", location_reached=True, item_attempts=2,
                pending_seed={"items": [{"item_name": "xyznonexistent", "quantity": 1, "preference": None}]},
            )
            ma.n_extract_items(s)
            self.assertEqual(s.next_node, "n_respond")
            self.assertTrue(s.went_idle)

    @patch.object(ma, "get_reply_or_route")
    def test_menu_handoff(self, mock_grr):
        mock_grr.return_value = fake_extract(intent="menu", category="starters")
        s = make_state(table_no="3", location_reached=True)
        ma.n_extract_items(s)
        self.assertEqual(s.next_node, "n_menu_pick_category")


class TestNodeAskQty(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        _install_fake_menu()

    @patch.object(ma, "get_reply_or_route")
    def test_qty_provided(self, mock_grr):
        mock_grr.return_value = fake_extract(quantity=3)
        s = make_state(active_item={
            "item_id": "1", "name": "Margherita Pizza", "category": "main_course",
            "quantity": None, "preference": None, "preference_asked": False,
        })
        ma.n_ask_qty(s)
        self.assertEqual(s.active_item["quantity"], 3)
        self.assertEqual(s.next_node, "n_ask_preference")

    @patch.object(ma, "get_reply_or_route")
    def test_qty_not_provided_retries(self, mock_grr):
        mock_grr.return_value = fake_extract()
        s = make_state(active_item={
            "item_id": "1", "name": "Margherita Pizza", "category": "main_course",
            "quantity": None, "preference": None, "preference_asked": False,
        })
        ma.n_ask_qty(s)
        self.assertEqual(s.next_node, "n_ask_qty")
        self.assertTrue(s.ask_retry)

    @patch.object(ma, "get_reply_or_route")
    def test_qty_skips_preference_if_already_asked(self, mock_grr):
        mock_grr.return_value = fake_extract(quantity=1)
        s = make_state(
            active_item={
                "item_id": "1", "name": "Margherita Pizza", "category": "main_course",
                "quantity": None, "preference": "extra cheese", "preference_asked": True,
            },
            pending_items=[],
        )
        ma.n_ask_qty(s)
        self.assertEqual(s.next_node, "n_ask_anything_else")


class TestNodeAskPreference(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        _install_fake_menu()

    @patch.object(ma, "get_reply_or_route")
    def test_preference_given(self, mock_grr):
        mock_grr.return_value = fake_extract(preference="extra spicy")
        s = make_state(
            active_item={
                "item_id": "1", "name": "Margherita Pizza", "category": "main_course",
                "quantity": 2, "preference": None, "preference_asked": False,
            },
            pending_items=[],
        )
        ma.n_ask_preference(s)
        # After preference is set, commit_and_continue runs (qty is set,
        # preference_asked becomes True) so active_item becomes None and
        # the item moves to order_items.
        self.assertIsNone(s.active_item)
        self.assertEqual(len(s.order_items), 1)
        self.assertEqual(s.order_items[0]["preference"], "extra spicy")
        self.assertEqual(s.next_node, "n_ask_anything_else")

    @patch.object(ma, "get_reply_or_route")
    def test_preference_declined(self, mock_grr):
        mock_grr.return_value = fake_extract(yes_no="no")
        s = make_state(
            active_item={
                "item_id": "1", "name": "Margherita Pizza", "category": "main_course",
                "quantity": 2, "preference": None, "preference_asked": False,
            },
            pending_items=[],
        )
        ma.n_ask_preference(s)
        self.assertIsNone(s.active_item)
        self.assertEqual(len(s.order_items), 1)
        self.assertIsNone(s.order_items[0]["preference"])

    @patch.object(ma, "get_reply_or_route")
    def test_preference_none_word(self, mock_grr):
        mock_grr.return_value = fake_extract(preference="none")
        s = make_state(
            active_item={
                "item_id": "1", "name": "Margherita Pizza", "category": "main_course",
                "quantity": 2, "preference": None, "preference_asked": False,
            },
            pending_items=[],
        )
        ma.n_ask_preference(s)
        self.assertIsNone(s.active_item)
        self.assertEqual(len(s.order_items), 1)
        self.assertIsNone(s.order_items[0]["preference"])


class TestNodeAskAnythingElse(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        _install_fake_menu()

    @patch.object(ma, "get_reply_or_route")
    def test_guest_says_no(self, mock_grr):
        mock_grr.return_value = fake_extract(yes_no="no")
        s = make_state(order_items=[{"name": "Pizza", "quantity": 1}])
        ma.n_ask_anything_else(s)
        self.assertEqual(s.next_node, "n_confirm_order")

    @patch.object(ma, "get_reply_or_route")
    def test_guest_says_yes(self, mock_grr):
        mock_grr.return_value = fake_extract(yes_no="yes")
        s = make_state()
        ma.n_ask_anything_else(s)
        self.assertEqual(s.next_node, "n_extract_items")

    @patch.object(ma, "get_reply_or_route")
    def test_guest_names_more_items(self, mock_grr):
        mock_grr.return_value = fake_extract(
            items=[{"item_name": "Fresh Lime Soda", "quantity": 1, "preference": None}],
        )
        s = make_state()
        ma.n_ask_anything_else(s)
        self.assertEqual(s.next_node, "n_extract_items")
        self.assertIsNotNone(s.pending_seed)


class TestNodeConfirmOrder(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        _install_fake_menu()

    @patch.object(ma, "get_reply_or_route")
    def test_confirm_yes(self, mock_grr):
        mock_grr.return_value = fake_extract(yes_no="yes")
        s = make_state(order_items=[
            {"item_id": "1", "name": "Margherita Pizza", "quantity": 2},
        ])
        ma.n_confirm_order(s)
        self.assertEqual(s.next_node, "n_finalize_order")

    @patch.object(ma, "get_reply_or_route")
    def test_confirm_no(self, mock_grr):
        mock_grr.return_value = fake_extract(yes_no="no")
        s = make_state(order_items=[
            {"item_id": "1", "name": "Margherita Pizza", "quantity": 2},
        ])
        ma.n_confirm_order(s)
        self.assertEqual(s.next_node, "n_confirm_reject")


class TestNodeConfirmReject(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        _install_fake_menu()

    @patch.object(ma, "get_reply_or_route")
    def test_cancel_choice(self, mock_grr):
        mock_grr.return_value = fake_extract(cancel_or_change="cancel")
        s = make_state()
        ma.n_confirm_reject(s)
        self.assertEqual(s.next_node, "n_cancel_order")

    @patch.object(ma, "get_reply_or_route")
    def test_change_choice(self, mock_grr):
        mock_grr.return_value = fake_extract(cancel_or_change="change")
        s = make_state()
        ma.n_confirm_reject(s)
        self.assertEqual(s.next_node, "n_edit_order_start")

    @patch.object(ma, "say")
    @patch.object(ma, "get_reply_or_route")
    def test_unclear_choice(self, mock_grr, mock_say):
        mock_grr.return_value = fake_extract(cancel_or_change="something_else")
        s = make_state()
        ma.n_confirm_reject(s)
        self.assertEqual(s.next_node, "n_confirm_reject")


class TestNodeCancelOrder(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        _install_fake_menu()

    @patch.object(ma, "say")
    def test_cancel_clears_order(self, mock_say):
        s = make_state(order_items=[
            {"item_id": "1", "name": "Margherita Pizza", "quantity": 2},
        ])
        ma.n_cancel_order(s)
        self.assertEqual(s.order_items, [])
        self.assertTrue(s.went_idle)
        self.assertEqual(s.next_node, "n_respond")


class TestNodeEditOrderStart(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        _install_fake_menu()

    @patch.object(ma, "say")
    def test_nothing_to_edit(self, mock_say):
        s = make_state(order_items=[])
        ma.n_edit_order_start(s)
        self.assertEqual(s.next_node, "n_respond")
        self.assertTrue(s.went_idle)

    @patch.object(ma, "get_reply_or_route")
    def test_edit_target_found(self, mock_grr):
        mock_grr.return_value = fake_extract(
            edit_target="Margherita Pizza", edit_action="change_qty", edit_value="3",
        )
        s = make_state(order_items=[
            {"item_id": "1", "name": "Margherita Pizza", "category": "main_course", "quantity": 2},
        ])
        ma.n_edit_order_start(s)
        self.assertEqual(s.next_node, "n_edit_apply")
        self.assertEqual(s.edit_target_name, "Margherita Pizza")

    @patch.object(ma, "say")
    @patch.object(ma, "get_reply_or_route")
    def test_edit_target_not_found(self, mock_grr, mock_say):
        mock_grr.return_value = fake_extract(edit_target="Unknown Dish")
        s = make_state(order_items=[
            {"item_id": "1", "name": "Margherita Pizza", "category": "main_course", "quantity": 2},
        ])
        ma.n_edit_order_start(s)
        self.assertEqual(s.next_node, "n_edit_order_start")


class TestNodeEditApply(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        _install_fake_menu()

    @patch.object(ma, "say")
    def test_remove_item(self, mock_say):
        item = {"item_id": "1", "name": "Margherita Pizza", "category": "main_course", "quantity": 2}
        s = make_state(
            order_items=[item],
            edit_target_name="Margherita Pizza",
            pending_seed={"edit_action": "remove"},
        )
        ma.n_edit_apply(s)
        self.assertEqual(len(s.order_items), 0)
        self.assertEqual(s.next_node, "n_confirm_order")

    @patch.object(ma, "say")
    def test_change_qty(self, mock_say):
        item = {"item_id": "1", "name": "Margherita Pizza", "category": "main_course", "quantity": 2}
        s = make_state(
            order_items=[item],
            edit_target_name="Margherita Pizza",
            pending_seed={"edit_action": "change_qty", "edit_value": "5"},
        )
        ma.n_edit_apply(s)
        self.assertEqual(item["quantity"], 5)
        self.assertEqual(s.next_node, "n_confirm_order")

    @patch.object(ma, "say")
    def test_change_preference(self, mock_say):
        item = {"item_id": "1", "name": "Margherita Pizza", "category": "main_course",
                "quantity": 2, "preference": "mild"}
        s = make_state(
            order_items=[item],
            edit_target_name="Margherita Pizza",
            pending_seed={"edit_action": "change_preference", "edit_value": "extra spicy"},
        )
        ma.n_edit_apply(s)
        self.assertEqual(item["preference"], "extra spicy")

    @patch.object(ma, "say")
    def test_replace_item(self, mock_say):
        item = {"item_id": "1", "name": "Margherita Pizza", "category": "main_course", "quantity": 2}
        s = make_state(
            order_items=[item],
            edit_target_name="Margherita Pizza",
            pending_seed={"edit_action": "replace", "edit_value": "Masala Dosa"},
        )
        ma.n_edit_apply(s)
        self.assertEqual(item["name"], "Masala Dosa")
        self.assertEqual(item["item_id"], "2")


class TestNodeFinalizeOrder(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        _install_fake_menu()

    @patch.object(ma, "db_place_order")
    @patch.object(ma, "say")
    def test_finalize_with_items(self, mock_say, mock_db):
        s = make_state(order_items=[
            {"item_id": "1", "name": "Margherita Pizza", "category": "main_course", "quantity": 2},
            {"item_id": "5", "name": "Fresh Lime Soda", "category": "beverages", "quantity": 1},
        ])
        ma.n_finalize_order(s)
        self.assertTrue(s.went_idle)
        self.assertEqual(s.next_node, "n_respond")
        self.assertEqual(s.order_total, 2 * 350.0 + 1 * 80.0)
        mock_db.assert_called_once()

    @patch.object(ma, "say")
    def test_finalize_empty_order(self, mock_say):
        s = make_state(order_items=[])
        ma.n_finalize_order(s)
        self.assertTrue(s.went_idle)
        self.assertEqual(s.next_node, "n_respond")


class TestNodeStub(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        _install_fake_menu()

    @patch.object(ma, "say")
    def test_stub_clears_seed(self, mock_say):
        s = make_state(pending_seed={"stub_intent": "get_bill"})
        ma.n_stub(s)
        self.assertIsNone(s.pending_seed)
        self.assertTrue(s.went_idle)
        self.assertEqual(s.next_node, "n_respond")


class TestNodeRespond(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        _install_fake_menu()

    @patch.object(ma, "db_end_session")
    def test_respond_resets_state(self, mock_db):
        s = make_state(pending_question="something", item_attempts=5)
        ma.n_respond(s)
        self.assertIsNone(s.pending_question)
        self.assertEqual(s.item_attempts, 0)
        self.assertEqual(s.next_node, "n_return_to_kitchen")


class TestNodeReturnToKitchen(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        _install_fake_menu()

    @patch.object(ma, "navigate_and_wait")
    @patch.object(ma, "report_phase")
    def test_return_navigates_when_at_table(self, mock_phase, mock_nav):
        s = make_state(table_no="3", location_reached=True)
        ma.n_return_to_kitchen(s)
        mock_nav.assert_called_once_with("Kitchen")
        self.assertIsNone(s.table_no)
        self.assertFalse(s.location_reached)
        self.assertEqual(s.next_node, "END")

    def test_return_skips_nav_when_not_at_table(self):
        s = make_state(table_no="3", location_reached=False)
        ma.n_return_to_kitchen(s)
        self.assertIsNone(s.table_no)
        self.assertEqual(s.next_node, "END")


class TestNodeDeliverAnnounce(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        _install_fake_menu()

    @patch.object(ma, "say")
    @patch.object(ma, "get_reply_or_route")
    def test_guest_confirms_pickup(self, mock_grr, mock_say):
        mock_grr.return_value = fake_extract(yes_no="yes")
        s = make_state(table_no="2")
        ma.n_deliver_announce(s)
        self.assertTrue(s.went_idle)
        self.assertEqual(s.next_node, "n_respond")

    @patch.object(ma, "get_reply_or_route")
    def test_guest_not_ready(self, mock_grr):
        mock_grr.return_value = fake_extract(yes_no="no")
        s = make_state(table_no="2")
        ma.n_deliver_announce(s)
        self.assertEqual(s.next_node, "n_deliver_announce")
        self.assertTrue(s.ask_retry)


class TestNodeMenuPickCategory(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        _install_fake_menu()

    @patch.object(ma, "get_reply_or_route")
    def test_category_found(self, mock_grr):
        mock_grr.return_value = fake_extract(category="starters")
        s = make_state()
        ma.n_menu_pick_category(s)
        self.assertEqual(s.active_menu_category, "starters")
        self.assertEqual(s.next_node, "n_menu_answer_questions")

    @patch.object(ma, "get_reply_or_route")
    def test_category_not_found_retries(self, mock_grr):
        mock_grr.return_value = fake_extract(category="xyz_invalid")
        s = make_state()
        ma.n_menu_pick_category(s)
        self.assertEqual(s.next_node, "n_menu_pick_category")
        self.assertTrue(s.ask_retry)

    @patch.object(ma, "get_reply_or_route")
    def test_order_handoff_from_menu(self, mock_grr):
        mock_grr.return_value = fake_extract(
            intent="take_order",
            items=[{"item_name": "Margherita Pizza", "quantity": 1, "preference": None}],
        )
        s = make_state()
        ma.n_menu_pick_category(s)
        self.assertEqual(s.next_node, "n_extract_items")


class TestNodeMenuAnswerQuestions(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        _install_fake_menu()

    @patch.object(ma, "get_reply_or_route")
    def test_first_visit_describes_category(self, mock_grr):
        mock_grr.return_value = fake_extract(yes_no="yes")
        s = make_state(active_menu_category="starters", category_intro_said=False)
        ma.n_menu_answer_questions(s)
        self.assertTrue(s.category_intro_said)
        self.assertEqual(s.next_node, "n_menu_answer_questions")

    @patch.object(ma, "say")
    @patch.object(ma, "get_reply_or_route")
    def test_guest_done_browsing(self, mock_grr, mock_say):
        mock_grr.return_value = fake_extract(yes_no="no")
        s = make_state(active_menu_category="starters", category_intro_said=True)
        ma.n_menu_answer_questions(s)
        self.assertTrue(s.went_idle)
        self.assertEqual(s.next_node, "n_respond")

    @patch.object(ma, "get_reply_or_route")
    def test_switch_category(self, mock_grr):
        mock_grr.return_value = fake_extract(category="desserts")
        s = make_state(active_menu_category="starters", category_intro_said=True)
        ma.n_menu_answer_questions(s)
        self.assertEqual(s.active_menu_category, "desserts")
        self.assertFalse(s.category_intro_said)

    @patch.object(ma, "get_reply_or_route")
    def test_order_handoff(self, mock_grr):
        mock_grr.return_value = fake_extract(
            intent="take_order",
            items=[{"item_name": "Paneer Tikka", "quantity": 2, "preference": None}],
        )
        s = make_state(active_menu_category="starters", category_intro_said=True)
        ma.n_menu_answer_questions(s)
        self.assertEqual(s.next_node, "n_extract_items")

    @patch.object(ma, "say")
    @patch.object(ma, "get_reply_or_route")
    def test_too_many_off_topic(self, mock_grr, mock_say):
        mock_grr.return_value = None
        s = make_state(active_menu_category="starters", menu_browse_off_topic_count=2)
        s.next_node = "n_menu_answer_questions"
        ma.n_menu_answer_questions(s)
        self.assertTrue(s.went_idle)
        self.assertEqual(s.next_node, "n_respond")


# ---------------------------------------------------------------------------
# End-to-end flow tests (drive through multiple nodes via direct calls)
# ---------------------------------------------------------------------------

class TestFullOrderFlow(unittest.TestCase):
    """Simulates the complete happy-path order flow:
    intent -> ask_table -> navigate -> greet -> extract_items ->
    ask_qty -> ask_preference -> ask_anything_else -> confirm -> finalize ->
    respond -> return_to_kitchen"""

    @classmethod
    def setUpClass(cls):
        _install_fake_menu()

    @patch.object(ma, "db_place_order")
    @patch.object(ma, "db_end_session")
    @patch.object(ma, "db_start_session")
    @patch.object(ma, "navigate_and_wait")
    @patch.object(ma, "report_phase")
    @patch.object(ma, "perform_travel_action", return_value=True)
    @patch.object(ma, "say")
    @patch.object(ma, "speak_text")
    @patch.object(ma, "render", return_value="Test prompt")
    @patch("sonic.main_agent.interrupt")
    @patch.object(ma, "interpret")
    def test_happy_path_single_item(self, mock_interpret, mock_interrupt,
                                     mock_render, mock_speak, mock_say,
                                     mock_travel, mock_phase, mock_nav,
                                     mock_db_start, mock_db_end, mock_db_order):
        # Step 1: Intent classify — user says "order food at table 3"
        mock_interrupt.return_value = "I want to order at table 3"
        def _interpret_side_effect(state, transcript, expects):
            state.table_no = "3"
            return fake_extract(
                intent="take_order", table_no="3",
                items=[{"item_name": "Margherita Pizza", "quantity": 2, "preference": "extra cheese"}],
            )
        mock_interpret.side_effect = _interpret_side_effect
        s = make_state()
        ma.n_intent_classify(s)
        self.assertEqual(s.next_node, "n_navigate_to_table")
        self.assertEqual(s.table_no, "3")

        # Step 2: Navigate to table
        ma.n_navigate_to_table(s)
        self.assertTrue(s.location_reached)
        self.assertEqual(s.next_node, "n_greet_and_ask_order")

        # Step 3: Greet — seed carries the items from intent classify
        ma.n_greet_and_ask_order(s)
        self.assertEqual(s.next_node, "n_extract_items")

        # Step 4: Extract items — single valid item, fully specified (qty=2,
        # pref="extra cheese") so it's committed immediately via
        # route_active_item -> commit_and_continue -> n_ask_anything_else
        ma.n_extract_items(s)
        self.assertIsNone(s.active_item)  # committed
        self.assertEqual(len(s.order_items), 1)
        self.assertEqual(s.order_items[0]["name"], "Margherita Pizza")
        self.assertEqual(s.next_node, "n_ask_anything_else")

        # Step 5: Anything else? No
        mock_interpret.reset_mock()
        mock_interrupt.return_value = "No, that's all"
        with patch.object(ma, "get_reply_or_route", return_value=fake_extract(yes_no="no")):
            ma.n_ask_anything_else(s)
        self.assertEqual(s.next_node, "n_confirm_order")

        # Step 6: Confirm order — yes
        with patch.object(ma, "get_reply_or_route", return_value=fake_extract(yes_no="yes")):
            ma.n_confirm_order(s)
        self.assertEqual(s.next_node, "n_finalize_order")

        # Step 7: Finalize
        ma.n_finalize_order(s)
        self.assertTrue(s.went_idle)
        self.assertEqual(s.order_total, 2 * 350.0)
        self.assertEqual(s.next_node, "n_respond")

        # Step 8: Respond (cleanup)
        ma.n_respond(s)
        self.assertEqual(s.next_node, "n_return_to_kitchen")

        # Step 9: Return to kitchen
        ma.n_return_to_kitchen(s)
        self.assertIsNone(s.table_no)
        self.assertEqual(s.next_node, "END")


class TestFullOrderWithEditFlow(unittest.TestCase):
    """Order -> confirm NO -> edit quantity -> re-confirm -> finalize"""

    @classmethod
    def setUpClass(cls):
        _install_fake_menu()

    @patch.object(ma, "db_place_order")
    @patch.object(ma, "db_end_session")
    @patch.object(ma, "db_start_session")
    @patch.object(ma, "navigate_and_wait")
    @patch.object(ma, "report_phase")
    @patch.object(ma, "perform_travel_action", return_value=True)
    @patch.object(ma, "say")
    @patch.object(ma, "speak_text")
    @patch.object(ma, "render", return_value="Prompt")
    @patch("sonic.main_agent.interrupt")
    @patch.object(ma, "interpret")
    def test_edit_then_confirm(self, mock_interpret, mock_interrupt, mock_render,
                                mock_speak, mock_say, mock_travel, mock_phase,
                                mock_nav, mock_db_start, mock_db_end, mock_db_order):
        # Setup: skip to greet (pretend we already navigated)
        s = make_state(table_no="3", location_reached=True,
                       pending_seed={"items": [{"item_name": "Margherita Pizza", "quantity": 2, "preference": None}]})
        ma.db_start_session = MagicMock()

        # Greet (seed present)
        ma.n_greet_and_ask_order(s)
        self.assertEqual(s.next_node, "n_extract_items")

        # Extract — qty=2, no preference yet
        ma.n_extract_items(s)
        self.assertEqual(s.active_item["name"], "Margherita Pizza")
        self.assertEqual(s.next_node, "n_ask_preference")

        # Preference — none
        with patch.object(ma, "get_reply_or_route", return_value=fake_extract(yes_no="no")):
            ma.n_ask_preference(s)
        self.assertIsNone(s.active_item)  # committed
        self.assertEqual(len(s.order_items), 1)
        self.assertEqual(s.next_node, "n_ask_anything_else")

        # Anything else? No
        with patch.object(ma, "get_reply_or_route", return_value=fake_extract(yes_no="no")):
            ma.n_ask_anything_else(s)
        self.assertEqual(s.next_node, "n_confirm_order")

        # Confirm -> NO
        with patch.object(ma, "get_reply_or_route", return_value=fake_extract(yes_no="no")):
            ma.n_confirm_order(s)
        self.assertEqual(s.next_node, "n_confirm_reject")

        # Reject choice -> change
        with patch.object(ma, "get_reply_or_route",
                          return_value=fake_extract(cancel_or_change="change")):
            ma.n_confirm_reject(s)
        self.assertEqual(s.next_node, "n_edit_order_start")

        # Edit start -> change qty of pizza to 5
        with patch.object(ma, "get_reply_or_route",
                          return_value=fake_extract(edit_target="Margherita Pizza",
                                                    edit_action="change_qty", edit_value="5")):
            ma.n_edit_order_start(s)
        self.assertEqual(s.next_node, "n_edit_apply")

        # Apply edit
        ma.n_edit_apply(s)
        self.assertEqual(s.order_items[0]["quantity"], 5)
        self.assertEqual(s.next_node, "n_confirm_order")

        # Confirm -> YES
        with patch.object(ma, "get_reply_or_route", return_value=fake_extract(yes_no="yes")):
            ma.n_confirm_order(s)
        self.assertEqual(s.next_node, "n_finalize_order")

        # Finalize
        ma.n_finalize_order(s)
        self.assertEqual(s.order_total, 5 * 350.0)


class TestFullOrderCancelFlow(unittest.TestCase):
    """Order -> confirm NO -> cancel"""

    @classmethod
    def setUpClass(cls):
        _install_fake_menu()

    @patch.object(ma, "db_end_session")
    @patch.object(ma, "say")
    @patch.object(ma, "speak_text")
    def test_cancel_after_reject(self, mock_speak, mock_say, mock_db_end):
        s = make_state(
            table_no="3", location_reached=True,
            order_items=[{"item_id": "1", "name": "Margherita Pizza", "quantity": 2}],
        )

        # Confirm -> NO
        with patch.object(ma, "get_reply_or_route", return_value=fake_extract(yes_no="no")):
            ma.n_confirm_order(s)
        self.assertEqual(s.next_node, "n_confirm_reject")

        # Cancel
        with patch.object(ma, "get_reply_or_route",
                          return_value=fake_extract(cancel_or_change="cancel")):
            ma.n_confirm_reject(s)
        self.assertEqual(s.next_node, "n_cancel_order")

        # Cancel clears order
        ma.n_cancel_order(s)
        self.assertEqual(s.order_items, [])
        self.assertTrue(s.went_idle)


class TestFullDeliveryFlow(unittest.TestCase):
    """Forced delivery: navigate -> announce -> confirm pickup -> respond -> return"""

    @classmethod
    def setUpClass(cls):
        _install_fake_menu()

    @patch.object(ma, "db_end_session")
    @patch.object(ma, "db_start_session")
    @patch.object(ma, "navigate_and_wait")
    @patch.object(ma, "report_phase")
    @patch.object(ma, "perform_travel_action", return_value=True)
    @patch.object(ma, "say")
    @patch.object(ma, "speak_text")
    def test_delivery_happy_path(self, mock_speak, mock_say, mock_travel,
                                  mock_phase, mock_nav, mock_db_start, mock_db_end):
        orig_loc, orig_act = ma.FORCED_LOCATION, ma.FORCED_ACTION
        try:
            ma.FORCED_LOCATION = "Table 2"
            ma.FORCED_ACTION = "deliver"

            # Intent classify -> navigate to table for delivery
            s = make_state(first_turn=True)
            ma.n_intent_classify(s)
            self.assertEqual(s.next_node, "n_navigate_to_table")
            self.assertEqual(s.post_arrival_node, "n_deliver_announce")

            # Navigate
            ma.n_navigate_to_table(s)
            self.assertTrue(s.location_reached)
            self.assertEqual(s.next_node, "n_deliver_announce")

            # Announce delivery — guest confirms pickup
            with patch.object(ma, "get_reply_or_route",
                              return_value=fake_extract(yes_no="yes")):
                ma.n_deliver_announce(s)
            self.assertTrue(s.went_idle)
            self.assertEqual(s.next_node, "n_respond")

            # Respond + return
            ma.n_respond(s)
            self.assertEqual(s.next_node, "n_return_to_kitchen")
            ma.n_return_to_kitchen(s)
            self.assertEqual(s.next_node, "END")
        finally:
            ma.FORCED_LOCATION = orig_loc
            ma.FORCED_ACTION = orig_act


class TestFullMenuBrowseToOrderFlow(unittest.TestCase):
    """Menu browse -> switch to ordering mid-browse"""

    @classmethod
    def setUpClass(cls):
        _install_fake_menu()

    @patch.object(ma, "db_place_order")
    @patch.object(ma, "db_end_session")
    @patch.object(ma, "db_start_session")
    @patch.object(ma, "navigate_and_wait")
    @patch.object(ma, "report_phase")
    @patch.object(ma, "perform_travel_action", return_value=True)
    @patch.object(ma, "say")
    @patch.object(ma, "speak_text")
    @patch.object(ma, "render", return_value="Prompt")
    @patch("sonic.main_agent.interrupt")
    @patch.object(ma, "interpret")
    def test_menu_browse_then_order(self, mock_interpret, mock_interrupt, mock_render,
                                     mock_speak, mock_say, mock_travel, mock_phase,
                                     mock_nav, mock_db_start, mock_db_end, mock_db_order):
        # Intent: menu
        mock_interrupt.return_value = "Show me the menu"
        mock_interpret.return_value = fake_extract(intent="menu")
        s = make_state()
        ma.n_intent_classify(s)
        self.assertEqual(s.next_node, "n_ask_table")

        # Ask table -> table 1
        with patch.object(ma, "get_reply_or_route", return_value=fake_extract(table_no="1")):
            s.table_no = "1"
            ma.n_ask_table(s)
        self.assertEqual(s.next_node, "n_navigate_to_table")

        # Navigate
        ma.n_navigate_to_table(s)
        self.assertEqual(s.next_node, "n_menu_pick_category")

        # Pick category -> starters
        with patch.object(ma, "get_reply_or_route", return_value=fake_extract(category="starters")):
            ma.n_menu_pick_category(s)
        self.assertEqual(s.active_menu_category, "starters")
        self.assertEqual(s.next_node, "n_menu_answer_questions")

        # Describe category, then guest wants to order
        with patch.object(ma, "get_reply_or_route",
                          return_value=fake_extract(
                              intent="take_order",
                              items=[{"item_name": "Paneer Tikka", "quantity": 2, "preference": None}],
                          )):
            ma.n_menu_answer_questions(s)
        self.assertEqual(s.next_node, "n_extract_items")

        # Extract items
        ma.n_extract_items(s)
        self.assertEqual(s.active_item["name"], "Paneer Tikka")

        # Preference -> no
        with patch.object(ma, "get_reply_or_route", return_value=fake_extract(yes_no="no")):
            ma.n_ask_preference(s)

        # Anything else -> no
        with patch.object(ma, "get_reply_or_route", return_value=fake_extract(yes_no="no")):
            ma.n_ask_anything_else(s)

        # Confirm -> yes
        with patch.object(ma, "get_reply_or_route", return_value=fake_extract(yes_no="yes")):
            ma.n_confirm_order(s)

        # Finalize
        ma.n_finalize_order(s)
        self.assertEqual(s.order_total, 2 * 250.0)
        self.assertTrue(s.went_idle)


class TestMultiItemOrderFlow(unittest.TestCase):
    """Guest orders multiple items in one utterance"""

    @classmethod
    def setUpClass(cls):
        _install_fake_menu()

    @patch.object(ma, "db_place_order")
    @patch.object(ma, "db_end_session")
    @patch.object(ma, "say")
    @patch.object(ma, "speak_text")
    def test_two_items_one_utterance(self, mock_speak, mock_say,
                                     mock_db_end, mock_db_order):
        s = make_state(
            table_no="3", location_reached=True,
            pending_seed={"items": [
                {"item_name": "Margherita Pizza", "quantity": 2, "preference": "extra cheese"},
                {"item_name": "Fresh Lime Soda", "quantity": 1, "preference": None},
            ]},
        )

        # Greet -> extract items
        ma.n_greet_and_ask_order(s)
        self.assertEqual(s.next_node, "n_extract_items")

        # First item (pizza) — qty=2, pref="extra cheese", so preference_asked=True
        # route_active_item -> commit_and_continue -> pending_items has 1 -> n_extract_items
        ma.n_extract_items(s)
        self.assertIsNone(s.active_item)  # committed immediately
        self.assertEqual(len(s.order_items), 1)
        self.assertEqual(s.order_items[0]["name"], "Margherita Pizza")
        self.assertEqual(s.next_node, "n_extract_items")  # continues to next pending item

        # Second item (lime soda) from pending queue — qty=1, no preference
        ma.n_extract_items(s)
        self.assertIsNotNone(s.active_item)
        self.assertEqual(s.active_item["name"], "Fresh Lime Soda")
        # qty=1, preference=None, preference_asked=False -> n_ask_preference
        self.assertEqual(s.next_node, "n_ask_preference")

        # Preference -> no
        with patch.object(ma, "get_reply_or_route", return_value=fake_extract(yes_no="no")):
            ma.n_ask_preference(s)
        self.assertEqual(len(s.order_items), 2)
        self.assertEqual(s.next_node, "n_ask_anything_else")

        # Anything else -> no
        with patch.object(ma, "get_reply_or_route", return_value=fake_extract(yes_no="no")):
            ma.n_ask_anything_else(s)
        self.assertEqual(s.next_node, "n_confirm_order")

        # Confirm -> yes
        with patch.object(ma, "get_reply_or_route", return_value=fake_extract(yes_no="yes")):
            ma.n_confirm_order(s)
        self.assertEqual(s.next_node, "n_finalize_order")

        # Finalize
        ma.n_finalize_order(s)
        self.assertEqual(s.order_total, 2 * 350.0 + 1 * 80.0)


# ---------------------------------------------------------------------------
# LLMClient unit tests (mocked HTTP)
# ---------------------------------------------------------------------------

class TestLLMClient(unittest.TestCase):

    @patch("sonic.main_agent.requests.post")
    def test_chat_success(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.json.return_value = {
            "choices": [{"message": {"content": "Hello!"}}]
        }
        mock_post.return_value = mock_resp
        client = LLMClient(base_url="http://fake", model="test", api_key="key")
        result = client.chat("system", "user")
        self.assertEqual(result, "Hello!")

    @patch("sonic.main_agent.requests.post")
    def test_chat_failure_raises(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.ok = False
        mock_resp.status_code = 500
        mock_resp.reason = "Server Error"
        mock_resp.text = "Internal error"
        mock_resp.json.side_effect = Exception("no json")
        mock_post.return_value = mock_resp
        client = LLMClient(base_url="http://fake", model="test", api_key="key")
        with self.assertRaises(RuntimeError):
            client.chat("system", "user")


# ---------------------------------------------------------------------------
# Graph build test — ensures the graph compiles without error
# ---------------------------------------------------------------------------

class TestGraphBuild(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        _install_fake_menu()

    def test_graph_compiles(self):
        app = build_graph()
        self.assertIsNotNone(app)


if __name__ == "__main__":
    unittest.main()
