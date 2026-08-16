"""Unit tests for the payload-driven agent's core state logic — no LLM, DB,
audio, or nav involved. Run with: python3 -m unittest test_payload_agent -v
"""

from __future__ import annotations

import unittest

from extraction import _clean_item_changes, _coerce_intent, _safe_int, build_system_prompt
from models import Intent, OrderItem, Payload
from payload_manager import (
    advance,
    calculate_total,
    mark_category_described,
    merge,
    resolve_item_changes,
    should_clear_history,
)
from prompt_generator import _build_prompt, clean_spoken_text, QUESTION_HINTS


class TestOrderItem(unittest.TestCase):
    def test_incomplete_without_name(self):
        self.assertFalse(OrderItem(serial_no=1, qty=2).is_complete())

    def test_incomplete_without_qty(self):
        self.assertFalse(OrderItem(serial_no=1, name="Coffee").is_complete())

    def test_incomplete_with_zero_qty(self):
        self.assertFalse(OrderItem(serial_no=1, name="Coffee", qty=0).is_complete())

    def test_complete(self):
        self.assertTrue(OrderItem(serial_no=1, name="Coffee", qty=2).is_complete())

    def test_complete_regardless_of_modifications(self):
        # Empty dict = no mods needed; a filled dict shouldn't matter either.
        self.assertTrue(OrderItem(serial_no=1, name="Coffee", qty=1).is_complete())
        self.assertTrue(
            OrderItem(serial_no=1, name="Coffee", qty=1, modifications={"size": "large"}).is_complete()
        )


class TestIsCompleteForIntent(unittest.TestCase):
    def test_none_intent_never_complete(self):
        self.assertFalse(Payload(intent=Intent.NONE).is_complete_for_intent())

    def test_take_order_needs_table(self):
        p = Payload(intent=Intent.TAKE_ORDER, robot_location=0)
        self.assertFalse(p.is_complete_for_intent())

    def test_take_order_needs_navigation(self):
        p = Payload(intent=Intent.TAKE_ORDER, order_table=3, robot_location=0)
        self.assertFalse(p.is_complete_for_intent())

    def test_take_order_needs_at_least_one_item(self):
        p = Payload(intent=Intent.TAKE_ORDER, order_table=3, robot_location=3)
        self.assertFalse(p.is_complete_for_intent())

    def test_take_order_needs_wants_more_answered(self):
        p = Payload(
            intent=Intent.TAKE_ORDER,
            order_table=3,
            robot_location=3,
            order_items={1: OrderItem(serial_no=1, name="Coffee", qty=1)},
        )
        self.assertFalse(p.is_complete_for_intent())

    def test_take_order_incomplete_while_wants_more_true(self):
        p = Payload(
            intent=Intent.TAKE_ORDER,
            order_table=3,
            robot_location=3,
            order_items={1: OrderItem(serial_no=1, name="Coffee", qty=1)},
            wants_more=True,
        )
        self.assertFalse(p.is_complete_for_intent())

    def test_take_order_needs_confirmation(self):
        p = Payload(
            intent=Intent.TAKE_ORDER,
            order_table=3,
            robot_location=3,
            order_items={1: OrderItem(serial_no=1, name="Coffee", qty=1)},
            wants_more=False,
        )
        self.assertFalse(p.is_complete_for_intent())

    def test_take_order_complete(self):
        p = Payload(
            intent=Intent.TAKE_ORDER,
            order_table=3,
            robot_location=3,
            order_items={1: OrderItem(serial_no=1, name="Coffee", qty=1)},
            wants_more=False,
            confirmed=True,
        )
        self.assertTrue(p.is_complete_for_intent())

    def test_take_order_not_complete_if_confirmed_false(self):
        p = Payload(
            intent=Intent.TAKE_ORDER,
            order_table=3,
            robot_location=3,
            order_items={1: OrderItem(serial_no=1, name="Coffee", qty=1)},
            wants_more=False,
            confirmed=False,
        )
        self.assertFalse(p.is_complete_for_intent())

    def test_tell_menu_not_complete_until_menu_done(self):
        p = Payload(intent=Intent.TELL_MENU)
        self.assertFalse(p.is_complete_for_intent())
        p.order_table = 2
        self.assertFalse(p.is_complete_for_intent())
        p.current_menu_category = "Coffee"
        self.assertFalse(p.is_complete_for_intent())  # still browsing — see TestMenuBrowsingLoop
        p.menu_done = True
        self.assertTrue(p.is_complete_for_intent())

    def test_navigate_complete_once_arrived(self):
        p = Payload(intent=Intent.NAVIGATE, order_table=5, robot_location=0)
        self.assertFalse(p.is_complete_for_intent())
        p.robot_location = 5
        self.assertTrue(p.is_complete_for_intent())

    def test_about_cafe_needs_only_intent(self):
        p = Payload(intent=Intent.ABOUT_CAFE)
        self.assertTrue(p.is_complete_for_intent())


class TestGetNextQuestion(unittest.TestCase):
    def test_take_order_asks_table_first(self):
        p = Payload(intent=Intent.TAKE_ORDER)
        self.assertEqual(p.get_next_question(), "which_table")

    def test_take_order_triggers_navigate(self):
        p = Payload(intent=Intent.TAKE_ORDER, order_table=3, robot_location=0)
        self.assertEqual(p.get_next_question(), "navigate")

    def test_take_order_asks_item_name(self):
        p = Payload(intent=Intent.TAKE_ORDER, order_table=3, robot_location=3)
        self.assertEqual(p.get_next_question(), "item_name")

    def test_take_order_asks_qty(self):
        p = Payload(
            intent=Intent.TAKE_ORDER,
            order_table=3,
            robot_location=3,
            order_items={1: OrderItem(serial_no=1, name="Coffee")},
        )
        self.assertEqual(p.get_next_question(), "item_qty")

    def test_take_order_asks_anything_else(self):
        p = Payload(
            intent=Intent.TAKE_ORDER,
            order_table=3,
            robot_location=3,
            order_items={1: OrderItem(serial_no=1, name="Coffee", qty=1)},
        )
        self.assertEqual(p.get_next_question(), "anything_else")

    def test_take_order_asks_confirm(self):
        p = Payload(
            intent=Intent.TAKE_ORDER,
            order_table=3,
            robot_location=3,
            order_items={1: OrderItem(serial_no=1, name="Coffee", qty=1)},
            wants_more=False,
        )
        self.assertEqual(p.get_next_question(), "confirm_order")

    def test_take_order_asks_confirm_again_if_confirmed_false(self):
        """Live-observed with gpt-4o-mini: a correction during confirm_order
        sometimes sets confirmed=false alongside the edit despite the prompt
        saying not to. get_next_question() must re-ask rather than return
        None here — returning None would desync from is_complete_for_intent()
        (still False), silently stranding the caller with nothing to do."""
        p = Payload(
            intent=Intent.TAKE_ORDER,
            order_table=3,
            robot_location=3,
            order_items={1: OrderItem(serial_no=1, name="Coffee", qty=1)},
            wants_more=False,
            confirmed=False,
        )
        self.assertEqual(p.get_next_question(), "confirm_order")
        self.assertFalse(p.is_complete_for_intent())

    def test_take_order_ready_when_complete(self):
        p = Payload(
            intent=Intent.TAKE_ORDER,
            order_table=3,
            robot_location=3,
            order_items={1: OrderItem(serial_no=1, name="Coffee", qty=1)},
            wants_more=False,
            confirmed=True,
        )
        self.assertIsNone(p.get_next_question())

    def test_tell_menu_asks_category(self):
        p = Payload(intent=Intent.TELL_MENU, order_table=2)
        self.assertEqual(p.get_next_question(), "menu_category")


class TestShouldClearHistory(unittest.TestCase):
    def test_no_change_no_clear(self):
        old = Payload(intent=Intent.TAKE_ORDER, order_table=3)
        self.assertFalse(should_clear_history(old, {"order_items": {1: {"name": "Coffee"}}}))

    def test_first_classification_no_clear(self):
        old = Payload(intent=Intent.NONE)
        self.assertFalse(should_clear_history(old, {"intent": Intent.TAKE_ORDER}))

    def test_intent_change_clears(self):
        old = Payload(intent=Intent.TELL_MENU, order_table=3)
        self.assertTrue(should_clear_history(old, {"intent": Intent.TAKE_ORDER}))

    def test_same_intent_no_clear(self):
        old = Payload(intent=Intent.TAKE_ORDER, order_table=3)
        self.assertFalse(should_clear_history(old, {"intent": Intent.TAKE_ORDER}))

    def test_table_change_clears(self):
        old = Payload(intent=Intent.TAKE_ORDER, order_table=3)
        self.assertTrue(should_clear_history(old, {"order_table": 5}))

    def test_table_first_set_no_clear(self):
        old = Payload(intent=Intent.TAKE_ORDER, order_table=None)
        self.assertFalse(should_clear_history(old, {"order_table": 3}))

    def test_same_table_no_clear(self):
        old = Payload(intent=Intent.TAKE_ORDER, order_table=3)
        self.assertFalse(should_clear_history(old, {"order_table": 3}))


class TestMerge(unittest.TestCase):
    def test_naming_item_infers_take_order_when_intent_unset(self):
        """Live-observed with gpt-4o-mini: 'can I have two coffees?' on the
        opening turn extracted item_changes but left intent null. Without
        this backstop, the payload gets stuck at intent=NONE forever —
        get_next_question() only advances once intent is known."""
        old = Payload()  # fresh session, intent=NONE
        merged = merge(old, {"order_items": {1: {"name": "Coffee", "qty": 2}}})
        self.assertEqual(merged.intent, Intent.TAKE_ORDER)

    def test_no_items_named_intent_stays_none(self):
        old = Payload()
        merged = merge(old, {})
        self.assertEqual(merged.intent, Intent.NONE)

    def test_explicit_intent_not_overridden_by_backstop(self):
        old = Payload()
        merged = merge(old, {"intent": Intent.TELL_MENU, "order_items": {1: {"name": "Coffee"}}})
        self.assertEqual(merged.intent, Intent.TELL_MENU)  # explicit wins, backstop doesn't fire

    def test_merge_preserves_history_on_same_intent(self):
        old = Payload(intent=Intent.TAKE_ORDER, order_table=3, robot_location=3)
        old.conversation_history.append({"role": "user", "text": "hi"})
        merged = merge(old, {"order_items": {1: {"name": "Coffee", "qty": 2}}})
        self.assertEqual(len(merged.conversation_history), 1)
        self.assertEqual(merged.order_items[1].name, "Coffee")
        self.assertEqual(merged.order_items[1].qty, 2)

    def test_merge_clears_history_on_intent_change(self):
        old = Payload(intent=Intent.TELL_MENU, order_table=3, robot_location=3)
        old.conversation_history.append({"role": "user", "text": "what's on the menu"})
        old.current_menu_category = "Coffee"
        merged = merge(
            old,
            {
                "intent": Intent.TAKE_ORDER,
                "order_items": {1: {"name": "Cappuccino (Hot)", "modifications": {"spice": "spicy"}}},
            },
        )
        self.assertEqual(merged.conversation_history, [])
        self.assertIsNone(merged.current_menu_category)  # reset — old intent's data
        self.assertEqual(merged.order_table, 3)  # carried forward — physical fact
        self.assertEqual(merged.robot_location, 3)
        self.assertEqual(merged.order_items[1].name, "Cappuccino (Hot)")
        self.assertEqual(merged.order_items[1].modifications, {"spice": "spicy"})

    def test_menu_to_order_handoff_preserves_item_from_same_utterance(self):
        """The exact 'I'll have a cappuccino' mid-menu-browsing scenario:
        intent switches AND an item is named in the same breath — the item
        must survive even though history/category reset."""
        old = Payload(intent=Intent.TELL_MENU, order_table=3, robot_location=0)
        old.current_menu_category = "Coffee"
        extracted = {
            "intent": Intent.TAKE_ORDER,
            "order_items": {1: {"name": "Cappuccino (Hot)", "qty": 1}},
        }
        merged = merge(old, extracted)
        self.assertEqual(merged.intent, Intent.TAKE_ORDER)
        self.assertTrue(merged.order_items[1].is_complete())

    def test_table_change_resets_items(self):
        old = Payload(
            intent=Intent.TAKE_ORDER,
            order_table=3,
            robot_location=3,
            order_items={1: OrderItem(serial_no=1, name="Coffee", qty=2)},
        )
        merged = merge(old, {"order_table": 5})
        self.assertEqual(merged.order_items, {})
        self.assertEqual(merged.order_table, 5)
        self.assertEqual(merged.robot_location, 3)  # still physically at 3 -> will re-navigate

    def test_qty_reduction_cancellation(self):
        """The demo scenario: guest says 'actually just one coffee' — qty
        drops from 2 to 1, same intent, no history clear."""
        old = Payload(
            intent=Intent.TAKE_ORDER,
            order_table=2,
            robot_location=2,
            order_items={
                1: OrderItem(serial_no=1, name="Coffee", qty=2),
                2: OrderItem(serial_no=2, name="Chocolate Chip Cookie", qty=1),
            },
            wants_more=False,
        )
        merged = merge(old, {"order_items": {1: {"qty": 1}}})
        self.assertEqual(merged.order_items[1].qty, 1)
        self.assertEqual(merged.order_items[2].qty, 1)  # untouched
        self.assertEqual(len(merged.order_items), 2)


class TestResolveItemChanges(unittest.TestCase):
    def test_fills_open_slot(self):
        p = Payload(order_items={1: OrderItem(serial_no=1)})
        resolved = resolve_item_changes(p, [{"name": "Coffee", "qty": 2}])
        self.assertEqual(resolved, {1: {"name": "Coffee", "qty": 2}})

    def test_no_open_slot_creates_new(self):
        p = Payload(order_items={1: OrderItem(serial_no=1, name="Coffee", qty=1)})
        resolved = resolve_item_changes(p, [{"name": "Cookie", "qty": 1}])
        self.assertEqual(resolved, {2: {"name": "Cookie", "qty": 1}})

    def test_matches_existing_named_item_not_just_the_last_one(self):
        """The demo cancellation case: cookie (serial 2) was added most
        recently, but 'actually just one coffee' must update serial 1
        (Coffee), not create/overwrite serial 2."""
        p = Payload(
            order_items={
                1: OrderItem(serial_no=1, name="Coffee", qty=2),
                2: OrderItem(serial_no=2, name="Chocolate Chip Cookie", qty=1),
            }
        )
        resolved = resolve_item_changes(p, [{"name": "Coffee", "qty": 1}])
        self.assertEqual(resolved, {1: {"name": "Coffee", "qty": 1}})

    def test_case_insensitive_name_match(self):
        p = Payload(order_items={1: OrderItem(serial_no=1, name="Coffee", qty=2)})
        resolved = resolve_item_changes(p, [{"name": "coffee", "qty": 1}])
        self.assertEqual(resolved, {1: {"name": "coffee", "qty": 1}})

    def test_multiple_new_items_one_utterance(self):
        p = Payload()
        resolved = resolve_item_changes(
            p, [{"name": "Pizza", "qty": 2}, {"name": "Coke", "qty": 1}]
        )
        self.assertEqual(resolved, {1: {"name": "Pizza", "qty": 2}, 2: {"name": "Coke", "qty": 1}})

    def test_qty_only_change_no_name(self):
        p = Payload(order_items={1: OrderItem(serial_no=1, name="Coffee")})
        resolved = resolve_item_changes(p, [{"qty": 3}])
        self.assertEqual(resolved, {1: {"qty": 3}})

    def test_modifications_merge(self):
        p = Payload(order_items={1: OrderItem(serial_no=1, name="Coffee", qty=1)})
        resolved = resolve_item_changes(p, [{"name": "Coffee", "modifications": {"size": "large"}}])
        self.assertEqual(resolved, {1: {"name": "Coffee", "modifications": {"size": "large"}}})

    def test_canonicalize_normalizes_menu_name(self):
        p = Payload()
        canon = {"cappucino": "Cappuccino (Hot)"}.get
        resolved = resolve_item_changes(p, [{"name": "cappucino", "qty": 1}], canonicalize=canon)
        self.assertEqual(resolved, {1: {"name": "Cappuccino (Hot)", "qty": 1}})

    def test_canonicalize_falls_back_to_raw_name_if_no_match(self):
        p = Payload()
        canon = lambda n: None  # no menu match
        resolved = resolve_item_changes(p, [{"name": "Burger"}], canonicalize=canon)
        self.assertEqual(resolved, {1: {"name": "Burger"}})

    def test_full_merge_integration(self):
        """resolve_item_changes() output feeds directly into merge()."""
        p = Payload(
            intent=Intent.TAKE_ORDER,
            order_table=2,
            robot_location=2,
            order_items={
                1: OrderItem(serial_no=1, name="Coffee", qty=2),
                2: OrderItem(serial_no=2, name="Chocolate Chip Cookie", qty=1),
            },
        )
        changes = resolve_item_changes(p, [{"name": "Coffee", "qty": 1}])
        merged = merge(p, {"order_items": changes})
        self.assertEqual(merged.order_items[1].qty, 1)
        self.assertEqual(merged.order_items[2].qty, 1)


class TestAdvance(unittest.TestCase):
    def test_wants_more_true_opens_new_slot(self):
        p = Payload(
            intent=Intent.TAKE_ORDER,
            order_table=3,
            robot_location=3,
            order_items={1: OrderItem(serial_no=1, name="Coffee", qty=1)},
            wants_more=True,
        )
        advanced = advance(p)
        self.assertIsNone(advanced.wants_more)
        self.assertIn(2, advanced.order_items)
        self.assertIsNone(advanced.order_items[2].name)
        self.assertEqual(advanced.get_next_question(), "item_name")

    def test_wants_more_false_no_change(self):
        p = Payload(
            intent=Intent.TAKE_ORDER,
            order_table=3,
            robot_location=3,
            order_items={1: OrderItem(serial_no=1, name="Coffee", qty=1)},
            wants_more=False,
        )
        advanced = advance(p)
        self.assertEqual(len(advanced.order_items), 1)

    def test_wants_more_true_with_item_named_same_turn_no_extra_slot(self):
        """'Yes, one cookie' — item_changes already added+completed the
        cookie via merge() before advance() runs. Opening a THIRD slot here
        would ask 'what dish?' again for something that was never asked
        for — the live bug this test guards against."""
        p = Payload(
            intent=Intent.TAKE_ORDER,
            order_table=2,
            robot_location=2,
            order_items={
                1: OrderItem(serial_no=1, name="Coffee", qty=2),
                2: OrderItem(serial_no=2, name="Chocolate Chip Cookie", qty=1),
            },
            wants_more=True,
        )
        advanced = advance(p, item_named_this_turn=True)
        self.assertIsNone(advanced.wants_more)
        self.assertEqual(len(advanced.order_items), 2)  # no phantom slot 3
        self.assertEqual(advanced.get_next_question(), "anything_else")

    def test_wants_more_true_item_named_but_still_incomplete(self):
        """'Yes, a coffee' with no qty yet — item_changes added it, but it's
        not complete, so the very next question should ask for qty, not
        loop back to anything_else and not open a redundant extra slot."""
        p = Payload(
            intent=Intent.TAKE_ORDER,
            order_table=2,
            robot_location=2,
            order_items={
                1: OrderItem(serial_no=1, name="Cookie", qty=1),
                2: OrderItem(serial_no=2, name="Coffee", qty=None),
            },
            wants_more=True,
        )
        advanced = advance(p, item_named_this_turn=True)
        self.assertEqual(len(advanced.order_items), 2)
        self.assertEqual(advanced.get_next_question(), "item_qty")


class TestCalculateTotal(unittest.TestCase):
    def test_total_with_price_lookup(self):
        prices = {"Coffee": 160.0, "Chocolate Chip Cookie": 75.0}
        p = Payload(
            order_items={
                1: OrderItem(serial_no=1, name="Coffee", qty=1),
                2: OrderItem(serial_no=2, name="Chocolate Chip Cookie", qty=1),
            }
        )
        total = calculate_total(p, lambda name: prices.get(name))
        self.assertEqual(total, 235.0)

    def test_total_skips_incomplete_items(self):
        p = Payload(order_items={1: OrderItem(serial_no=1, name="Coffee", qty=None)})
        total = calculate_total(p, lambda name: 160.0)
        self.assertEqual(total, 0.0)


class TestMenuBrowsingLoop(unittest.TestCase):
    def test_menu_not_complete_until_menu_done_true(self):
        p = Payload(intent=Intent.TELL_MENU, order_table=2, current_menu_category="Coffee")
        self.assertFalse(p.is_complete_for_intent())

    def test_flow_table_then_category_then_describe_then_next_step(self):
        p = Payload(intent=Intent.TELL_MENU)
        self.assertEqual(p.get_next_question(), "which_table")

        p = merge(p, {"order_table": 2})
        self.assertEqual(p.get_next_question(), "menu_category")

        p = merge(p, {"current_menu_category": "Coffee"})
        self.assertFalse(p.menu_category_described)
        self.assertEqual(p.get_next_question(), "describe_category")

        mark_category_described(p)
        self.assertEqual(p.get_next_question(), "menu_next_step")

    def test_different_category_resets_description_flag(self):
        p = Payload(
            intent=Intent.TELL_MENU,
            order_table=2,
            current_menu_category="Coffee",
            menu_category_described=True,
            menu_done=False,
        )
        p = merge(p, {"current_menu_category": "Bakery"})
        self.assertFalse(p.menu_category_described)
        self.assertIsNone(p.menu_done)
        self.assertEqual(p.get_next_question(), "describe_category")

    def test_same_category_again_does_not_reset(self):
        p = Payload(
            intent=Intent.TELL_MENU,
            order_table=2,
            current_menu_category="Coffee",
            menu_category_described=True,
            menu_done=False,
        )
        p = merge(p, {"current_menu_category": "Coffee"})
        self.assertTrue(p.menu_category_described)  # unchanged — not a real switch

    def test_menu_done_true_completes(self):
        p = Payload(
            intent=Intent.TELL_MENU,
            order_table=2,
            current_menu_category="Coffee",
            menu_category_described=True,
        )
        p = merge(p, {"menu_done": True})
        self.assertTrue(p.is_complete_for_intent())
        self.assertIsNone(p.get_next_question())

    def test_menu_to_order_handoff_mid_browsing(self):
        """The 'satisfy until order made, cancelled, or said no' requirement:
        guest browsing Coffee category says 'I'll have a cappuccino' —
        intent switches to take_order, menu state resets via should_clear."""
        p = Payload(
            intent=Intent.TELL_MENU,
            order_table=2,
            robot_location=2,
            current_menu_category="Coffee",
            menu_category_described=True,
        )
        merged = merge(
            p,
            {
                "intent": Intent.TAKE_ORDER,
                "order_items": {1: {"name": "Cappuccino (Hot)", "qty": 1}},
            },
        )
        self.assertEqual(merged.intent, Intent.TAKE_ORDER)
        self.assertIsNone(merged.current_menu_category)  # reset — menu-specific
        self.assertTrue(merged.order_items[1].is_complete())
        self.assertEqual(merged.order_table, 2)  # carried forward


class TestFullOrderScenario(unittest.TestCase):
    """End-to-end simulation of the Table 2 demo: dispatch -> auto-nav ->
    2 coffee -> 1 cookie -> cancel 1 coffee -> confirm -> finalize."""

    def test_table_2_dispatch_order_with_cancellation(self):
        # 1. Dispatch init
        payload = Payload(intent=Intent.TAKE_ORDER, order_table=2, robot_location=0)
        self.assertEqual(payload.get_next_question(), "navigate")

        # 2. Auto-navigate
        payload.robot_location = 2
        self.assertEqual(payload.get_next_question(), "item_name")

        # 3. "Can I have two coffees please?"
        payload = merge(payload, {"order_items": {1: {"name": "Coffee", "qty": 2}}})
        payload = advance(payload)
        self.assertEqual(payload.get_next_question(), "anything_else")

        # 4. "Yes, one cookie" -> wants_more True + new item in same turn
        payload = merge(
            payload,
            {"wants_more": True, "order_items": {2: {"name": "Chocolate Chip Cookie", "qty": 1}}},
        )
        payload = advance(payload)
        # advance() would open slot 3 since wants_more was True and slot 2
        # already has a real item from this same utterance — that's fine,
        # get_next_question sees slot 3 has no name and asks again.
        self.assertEqual(payload.get_next_question(), "item_name")

        # Guest has nothing more for slot 3 — say no on the next ask instead.
        payload = merge(payload, {"wants_more": False})
        # slot 3 (empty) blocks completion until we ask & they decline —
        # simulate the "item_name" question getting an empty/no response
        # by just dropping the empty slot before re-checking (mirrors what
        # main_agent_v2's loop does when item_name gets no dish named and
        # the guest's utterance was actually answering anything_else instead).
        empty_slots = [sn for sn, item in payload.order_items.items() if not item.name]
        for sn in empty_slots:
            del payload.order_items[sn]
        self.assertEqual(payload.get_next_question(), "confirm_order")

        # 5. Guest: "actually just one coffee" -> cancellation
        payload = merge(payload, {"order_items": {1: {"qty": 1}}})
        self.assertEqual(payload.order_items[1].qty, 1)
        self.assertEqual(payload.order_items[2].qty, 1)
        # confirmed should have reset to None since this looks like new
        # info after wants_more was already answered — in the real
        # extraction layer this comes from expects="confirm_order" catching
        # a correction instead of a yes/no; here we simulate by clearing it.
        payload.confirmed = None
        self.assertEqual(payload.get_next_question(), "confirm_order")

        # 6. Guest confirms
        payload = merge(payload, {"confirmed": True})
        self.assertTrue(payload.is_complete_for_intent())
        self.assertIsNone(payload.get_next_question())

        # 7. Finalize
        prices = {"Coffee": 160.0, "Chocolate Chip Cookie": 75.0}
        total = calculate_total(payload, lambda name: prices.get(name))
        self.assertEqual(total, 235.0)
        self.assertEqual(payload.order_summary(), "1x Coffee, 1x Chocolate Chip Cookie")


class TestExtractionHelpers(unittest.TestCase):
    def test_coerce_intent_valid(self):
        self.assertEqual(_coerce_intent("take_order"), Intent.TAKE_ORDER)

    def test_coerce_intent_invalid_returns_none(self):
        self.assertIsNone(_coerce_intent("nonsense_intent"))

    def test_coerce_intent_none_input(self):
        self.assertIsNone(_coerce_intent(None))

    def test_safe_int_from_string(self):
        self.assertEqual(_safe_int("3"), 3)

    def test_safe_int_from_none(self):
        self.assertIsNone(_safe_int(None))

    def test_safe_int_from_garbage(self):
        self.assertIsNone(_safe_int("three"))  # LLM is told to convert; this is the safety net

    def test_clean_item_changes_drops_empty_entries(self):
        raw = [{"name": None, "qty": None, "modifications": {}}, {"name": "Coffee", "qty": 1}]
        cleaned = _clean_item_changes(raw)
        self.assertEqual(len(cleaned), 1)
        self.assertEqual(cleaned[0]["name"], "Coffee")

    def test_clean_item_changes_stringifies_modifications(self):
        raw = [{"name": "Coffee", "modifications": {"size": "large"}}]
        cleaned = _clean_item_changes(raw)
        self.assertEqual(cleaned[0]["modifications"], {"size": "large"})

    def test_clean_item_changes_non_list_returns_empty(self):
        self.assertEqual(_clean_item_changes(None), [])
        self.assertEqual(_clean_item_changes("not a list"), [])

    def test_build_system_prompt_includes_stage_guidance(self):
        p = Payload(intent=Intent.TAKE_ORDER, order_table=2)
        prompt = build_system_prompt(p, "item_qty")
        self.assertIn("item_qty", prompt)
        self.assertIn("how many", prompt.lower())

    def test_build_system_prompt_includes_current_state(self):
        p = Payload(intent=Intent.TAKE_ORDER, order_table=3)
        prompt = build_system_prompt(p, "item_name")
        self.assertIn("order_table=3", prompt)


class TestPromptGenerator(unittest.TestCase):
    def test_clean_spoken_text_strips_quotes(self):
        self.assertEqual(clean_spoken_text('"Hello there!"'), "Hello there!")

    def test_clean_spoken_text_capitalizes(self):
        self.assertEqual(clean_spoken_text("hello there"), "Hello there")

    def test_clean_spoken_text_empty(self):
        self.assertEqual(clean_spoken_text(None), "")
        self.assertEqual(clean_spoken_text(""), "")

    def test_build_prompt_formats_context(self):
        p = Payload()
        prompt = _build_prompt(QUESTION_HINTS, "item_qty", p, item_name="Coffee")
        self.assertIn("Coffee", prompt)

    def test_build_prompt_survives_missing_context(self):
        p = Payload()
        # item_qty's hint needs {item_name} but we don't pass it — should not crash
        prompt = _build_prompt(QUESTION_HINTS, "item_qty", p)
        self.assertIsInstance(prompt, str)

    def test_build_prompt_includes_history(self):
        p = Payload()
        p.conversation_history.append({"role": "user", "text": "hello"})
        prompt = _build_prompt(QUESTION_HINTS, "which_table", p)
        self.assertIn("hello", prompt)


if __name__ == "__main__":
    unittest.main()
