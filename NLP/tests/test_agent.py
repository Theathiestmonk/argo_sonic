import asyncio

import pytest

from ..config import load_settings
from ..fastpath import try_fast
from ..knowledge import Menu, Restaurant, Waypoints, similarity
from ..robot.base import MoveResult
from ..robot.mock import MockRobot
from ..voice.tts import clean_for_tts, sentence_chunks
from .harness import ent, run_script

S = load_settings()


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------- knowledge
def test_waypoint_resolution():
    w = Waypoints(S, Restaurant(S).data["location_aliases"])
    assert w.resolve("table two")[0] == "Table 2"
    assert w.resolve("Table 3")[0] == "Table 3"
    assert w.resolve("base")[0] == "Docker"
    assert w.resolve("the kitchen")[0] == "Kitchen"
    name, sugg = w.resolve("counter")
    assert name is None
    assert w.resolve("xyzzy plugh") == (None, None)


def test_menu_lookup():
    r = Restaurant(S); m = Menu(S, r)
    assert m.find_item("butter chicken")[0].name == "Butter Chicken"
    assert m.find_item("hyderabadi chicken biryani")[0].name == "Hyderabadi Chicken Dum Biryani"
    assert m.resolve_categories("desserts") == ["Desserts"]
    assert m.resolve_categories("curries") == ["Curries"]
    assert "Starters" in m.resolve_categories("snacks")
    assert m.resolve_categories("biryani") == ["Biryani & Rice"]
    assert m.specials() and m.famous()
    assert m.currency == "INR" and m.money(249) == "249 rupees"
    assert any("Paneer" in i.name for i in m.search("paneer", k=6))
    assert all(i.diet == "veg" for i in m.search("paneer", diet="veg"))
    assert r.service_item("a glass of water") == "Water"
    assert r.service_item("butter chicken") is None


def test_similarity_basics():
    assert similarity("table two", "Table 2") == 1.0
    assert similarity("veg burger", "Veggie Burger") > 0.5


# ---------------------------------------------------------------- fast path
@pytest.mark.parametrize("text,aw,expect", [
    ("move forward", None, ("movement", "forward")),
    ("go back", None, ("movement", "backward")),
    ("turn left", None, ("movement", "turn_left")),
    ("move left please", None, ("movement", "left")),
    ("do a spin", None, ("movement", "spin")),
    ("bye", None, ("end", None)),
])
def test_fast_commands(text, aw, expect):
    e = try_fast(text, aw)
    assert (e["intent"], e["movement_direction"]) == expect


def test_fast_negatives():
    assert try_fast("go to the left table", None) is None
    assert try_fast("right", None) is None
    assert try_fast("yes", None) is None            # nothing awaited -> LLM decides
    assert try_fast("yes please", "order_more")["reply"] == "yes"
    assert try_fast("no that's all", "order_more")["reply"] == "no"


# ---------------------------------------------------------------- speech chunking
def test_sentence_chunker_first_chunk_breaks_early():
    async def toks():
        for t in "Sure thing my friend, I can help you with that right now. Anything else I can do? Great".split(" "):
            yield t + " "
    out = run(_collect(sentence_chunks(toks())))
    assert out[0].endswith(",") and len(out) >= 3 and out[-1] == "Great"


async def _collect(ait):
    return [x async for x in ait]


def test_tts_cleaning():
    assert clean_for_tts("**Hi** 🤖 there_") == "Hi there"


# ---------------------------------------------------------------- full graph flows
def last(voice, n=1):
    return voice.said[-n:]


def test_navigation_known_unknown_and_suggestion():
    v, r, _ = run(run_script([
        ("go to table two", ent(intent="navigation", location="Table 2")),
        ("go to the counter", ent(intent="navigation", location="counter")),
        ("where", ent(intent="navigation")),
        ("kitchen", ent(intent="navigation", location="kitchen")),
        ("go", ent(intent="navigation")),
    ]))
    said = " | ".join(v.said)
    assert "Going to Table 2." in v.said and "I'm at Table 2." in v.said
    assert "Where do you want me to go?" in v.said                     # location missing
    assert "Going to Kitchen." in v.said
    assert any("couldn't find counter" in s or "don't know how to go to counter" in s for s in v.said), said
    assert r.log[:1] == ["navigate Table 2"]


def test_unknown_location_suggests_best_match():
    v, r, _ = run(run_script([
        ("take me to table nine", ent(intent="navigation", location="table nine")),
        ("yes", ent()),
    ]))
    sugg = [s for s in v.said if "instead?" in s]
    assert sugg and "don't know how to go to table nine" in sugg[0], v.said
    target = sugg[0].split("go to ")[-1].split(" instead")[0]
    assert target.startswith("Table") and target != "Table 9"
    assert f"Going to {target}." in v.said


def test_typo_still_resolves():
    w = Waypoints(S)
    assert w.resolve("tabel four")[0] == "Table 4"
    assert w.resolve("table 9")[0] is None and w.resolve("table 9")[1].startswith("Table")


def test_ordering_full_flow_sends_order_to_remembered_table():
    v, r, sink = run(run_script([
        ("go to table three", ent(intent="navigation", location="Table 3")),
        ("i want to order", ent(intent="ordering")),
        ("two butter chicken", ent(intent="ordering", items=[{"name": "Butter Chicken", "quantity": 2, "variation": None}])),
        ("spicy", ent()),                                              # fast path: variation option
        ("and jeera rice and water", ent(intent="ordering", items=[{"name": "Water", "quantity": None, "variation": None},
                                                                {"name": "Jeera Rice", "quantity": 1, "variation": None}])),
        ("no", ent()),
        ("yes", ent()),
    ]))
    said = " | ".join(v.said)
    assert "Which table" not in said                                  # table 3 remembered from navigation
    assert "What would you like to order?" in v.said
    assert "Which spice level would you like for the Butter Chicken? Mild or Medium or Spicy?" in v.said
    assert any("Added 1 Water" in s for s in v.said)
    assert any("for table 3. Shall I place the order?" in s for s in v.said)
    assert len(sink.orders) == 1
    table, lines = sink.orders[0]
    assert table == 3
    assert {(l["name"], l["qty"]) for l in lines} >= {("Butter Chicken", 2), ("Water", 1)}


def test_ordering_asks_only_for_missing_slots():
    v, r, sink = run(run_script([
        ("order two veg seekh kebab for table 5", ent(intent="ordering", table_number=5,
         items=[{"name": "Veg Seekh Kebab", "quantity": 2, "variation": None}])),    # nothing missing: kebab has no variation
        ("a cold coffee", ent(intent="ordering", items=[{"name": "Cold Coffee", "quantity": None, "variation": None}])),
        ("3", ent()),                                                  # fast qty
        ("large", ent()),                                              # fast variation
    ]))
    assert "Added 2 Veg Seekh Kebab. Anything else?" in v.said
    assert "How many Cold Coffee would you like?" in v.said
    assert any(s.startswith("Which size") for s in v.said)
    assert "Added 3 Cold Coffee (Large). Anything else?" in v.said


def test_ordering_asks_table_when_unknown_and_unavailable_item():
    v, _, sink = run(run_script([
        ("two paneer tikka", ent(intent="ordering", items=[{"name": "Paneer Tikka", "quantity": 2, "variation": None},
                                                             {"name": "Cheese Fries", "quantity": 1, "variation": None}])),
        ("table nine", ent(intent="ordering", table_number=9)),
        ("two", ent(intent="ordering", table_number=2)),
    ]))
    assert v.said.count("Which table is this for?") == 1
    assert any("couldn't find Cheese Fries" in s for s in v.said)
    assert any("don't see a table 9" in s for s in v.said)
    assert "Added 2 Paneer Tikka. Anything else?" in v.said


def test_movement_ok_blocked_and_no_sensor(monkeypatch):
    class R(MockRobot):
        async def move(self, d):
            await asyncio.sleep(0.3)
            return {"left": MoveResult("blocked"), "backward": MoveResult("no_sensors")}.get(d, MoveResult("ok", 1.0))
    v, _, _ = run(run_script([
        ("move forward", ent()), ("move left please", ent()), ("go back", ent()),
    ], robot=R()))
    assert "Moving forward." in v.said
    assert v.said.count("Sorry, I am blocked by something in that direction and cannot move.") == 1
    assert any("safety sensors" in s for s in v.said)


def test_menu_to_order_handoff():
    v, _, sink = run(run_script([
        ("what is the menu", ent(intent="menu")),
        ("starters", ent()),                                          # fast: category
        ("tell me about the paneer tikka", ent(intent="menu", menu_item="Paneer Tikka")),
        ("yes", ent()),                                               # -> ordering handoff
        ("table 2", ent()),                                           # fast table
        ("2", ent()),
        ("no", ent()),
        ("yes", ent()),
    ]))
    said = " | ".join(v.said)
    assert "Which category would you like to hear about?" in said
    assert any(s.startswith("Under Starters we have") for s in v.said)
    assert any("Would you like to order it?" in s for s in v.said)
    assert sink.orders and sink.orders[0][0] == 2 and sink.orders[0][1][0]["name"] == "Paneer Tikka"


def test_menu_followups_no_then_no():
    v, _, _ = run(run_script([
        ("tell me about paneer tikka", ent(intent="menu", menu_item="Paneer Tikka")),
        ("no", ent()), ("no", ent()),
    ]))
    assert "No problem. Would you like to know about another item?" in v.said
    assert "Alright! Let me know whenever you'd like to order." in v.said


def test_general_then_order_prompt():
    v, _, _ = run(run_script([
        ("tell me a joke", ent(intent="general", general_topic="joke")),
        ("no", ent()),
    ]))
    assert any(s.startswith("[LLM:tell me a joke]") for s in v.said)
    assert any("order" in s.lower() for s in v.said[-2:])


def test_thats_all_does_not_end_session_midorder():
    # LLM (wrongly) says intent=end for "that's all" while we asked "Anything else?" – must be treated as "no".
    v, _, sink = run(run_script([
        ("order two butter chicken table 4", ent(intent="ordering", table_number=4,
         items=[{"name": "Butter Chicken", "quantity": 2, "variation": "mild"}])),
        ("that is it thanks mate", ent(intent="end")),
        ("yes", ent()),
    ]))
    assert sink.orders and sink.orders[0][0] == 4


def test_silence_then_sleep():
    v, _, _ = run(run_script([("", ent()), ("", ent())]))
    assert "<chirp>" in v.said
    assert "Okay, call me if you need anything." in v.said


def test_ambiguous_dish_asks_which_one():
    v, _, sink = run(run_script([
        ("order a kebab for table 3", ent(intent="ordering", table_number=3, items=[{"name": "kebab", "quantity": 1, "variation": None}])),
        ("the mutton one", ent(intent="ordering", items=[{"name": "Mutton Seekh Kebab", "quantity": 1, "variation": None}])),
    ]))
    ask = [s for s in v.said if s.endswith("Which one would you like?")]
    assert ask and "Hara Bhara Kebab" in ask[0] and "Mutton Seekh Kebab" in ask[0], v.said
    assert "Added 1 Mutton Seekh Kebab. Anything else?" in v.said


def test_misspelled_dish_and_no_silly_suggestion():
    r = Restaurant(S); m = Menu(S, r)
    assert m.find_item("chicken biriyani")[0].name == "Hyderabadi Chicken Dum Biryani"
    assert m.find_item("cheese fries") == (None, None)
    assert m.find_item("pizza") == (None, None)
    assert [i.name for i in m.ambiguous("naan")] == ["Butter Naan", "Garlic Naan"]
    assert m.ambiguous("butter chicken") == []


def test_menu_item_ambiguous_lists_options():
    v, _, _ = run(run_script([("tell me about biryani", ent(intent="menu", menu_item="biryani"))]))
    assert any("Which one would you like to know about?" in s and "Mutton Dum Biryani" in s for s in v.said), v.said


def test_wake_word_mid_action_cancels_and_restarts_keeping_cart():
    """'Hi Sonic' while busy → run cancelled (robot goal too), graph restarts with order/table intact."""
    import asyncio
    from ..agent import _run_once

    class V:
        def __init__(self): self.fire, self.interrupted = asyncio.Event(), 0
        async def barge_in(self): await self.fire.wait(); self.fire.clear()
        def interrupt(self): self.interrupted += 1

    cancelled = []
    cart = [{"name": "Burger", "qty": 2}]

    class G:
        async def astream(self, state, **k):
            yield {**state, "order": cart, "table": 4}   # state after the last finished node
            try:
                await asyncio.sleep(30)                  # "navigating" / "reading the menu"
            except asyncio.CancelledError:
                cancelled.append(1)
                raise

    async def go():
        v = V()
        asyncio.get_running_loop().call_later(0.05, v.fire.set)
        return v, await _run_once(G(), v, {"order": [], "pending": [], "table": None})

    v, nxt = run(go())
    assert cancelled == [1] and v.interrupted == 1
    assert nxt["order"] == cart and nxt["table"] == 4 and nxt["resumed"] is True


def test_wake_node_keeps_cart_when_resumed():
    from ..nodes.core import make_core
    class C:
        class voice:
            @staticmethod
            async def wait_wake(): ...
    wake = make_core(C)["wake"]
    out = run(wake({"resumed": True, "order": [1]}))
    assert "order" not in out and "pending" not in out and out["resumed"] is False
    assert run(wake({"order": [1]}))["order"] == []     # normal wake = new customer


# ---------------------------------------------------------------- voice tasks: "go to table N and <job>"
@pytest.mark.parametrize("text,loc,task", [
    ("go to table 1 and take order", "table 1", "take_order"),
    ("Go to table 4 and deliver the order.", "table 4", "deliver"),
    ("please come to table two then take the order", "table two", "take_order"),
    ("go to table 3 and take an order", "table 3", "take_order"),
    ("go to table 5 and serve the food", "table 5", "deliver"),
    ("head to table 2 and deliver order", "table 2", "deliver"),
])
def test_compound_fastpath(text, loc, task):
    e = try_fast(text, None)
    assert e and e["intent"] == "navigation" and e["location"] == loc and e["task"] == task


def test_take_order_task_goes_there_then_asks_for_the_order():
    voice, robot, sink = run(run_script([
        ("go to table 1 and take order", {}),            # fast path – no LLM
        ("two butter chicken", ent(intent="ordering", items=[{"name": "butter chicken", "quantity": 2, "variation": None}])),
        ("spicy", {}), ("no", {}), ("yes", {}),
    ]))
    said = " | ".join(voice.said)
    assert "Going to Table 1" in said and "What would you like to order" in said
    assert "Which table" not in said                          # table came from the navigation
    assert sink.orders and sink.orders[0][0] == 1


def test_deliver_task_announces_waits_for_done_and_returns_home():
    voice, robot, sink = run(run_script([
        ("go to table 4 and deliver the order", {}),
        ("done", {}),
    ]))
    said = " | ".join(voice.said)
    assert "Going to Table 4" in said and "your order is here" in said
    assert "Enjoy your meal" in said and "Heading back to Docker" in said
    assert robot.at is not None if hasattr(robot, "at") else True


def test_deliver_with_unknown_table_keeps_task_through_suggestion():
    voice, robot, sink = run(run_script([
        ("go to table 9 and deliver the order", {}),
        ("yes", {}),
    ]))
    said = " | ".join(voice.said)
    assert "Do you want me to go to" in said and "your order is here" in said


def test_deliver_without_place_asks_which_table():
    voice, robot, sink = run(run_script([
        ("deliver the order", {}),
        ("table 2", ent(intent="navigation", location="table 2")),
    ]))
    said = " | ".join(voice.said)
    assert "Which table should I deliver to" in said and "Going to Table 2" in said and "your order is here" in said


def test_pending_task_is_dropped_when_topic_changes():
    voice, robot, sink = run(run_script([
        ("go to table 9 and take order", {}),
        ("tell me a joke", ent(intent="general", general_topic="joke")),
        ("go to table 2", ent(intent="navigation", location="table 2")),
    ]))
    said = " | ".join(voice.said)
    assert "What would you like to order" not in said
