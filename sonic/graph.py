"""
LangGraph implementation of the Sonic robot NLP pipeline.

Mirrors the flowchart:

  Wake Word ("Hi Sonic") -> Intent Classify (LLM) -> one of 8 intents:
    menu | call_people | take_order | navigation | cutlery | about |
    normal_conv | get_bill

  take_order is the deepest branch:
    new_order   -> ask_item -> ask_qty -> ask_preference -> ask_to_add
                   -> (yes: loop to ask_item) (no: confirm_order -> place_order)
    edit_order  -> ask_item -> replace | customize | cancel_single | cancel_full
    cancel_order-> delete -> ask_item -> cancel
    add_item    -> ask_item (re-enters the same ask_item/ask_qty/ask_preference chain)

Each node is a plain function of (state) -> partial state update, so any
node body can be swapped for a real LLM call, TTS/ASR hook, or robot
actuator call without touching the graph wiring.
"""

from langgraph.graph import StateGraph, END
from schema import NLPState


# ---------------------------------------------------------------------------
# Entry nodes
# ---------------------------------------------------------------------------

def wake_word_listener(state: NLPState) -> dict:
    """Confirms 'Hi Sonic' was heard. In production this is an always-on
    ASR keyword spotter; here it just gates the pipeline."""
    return {"wake_word_detected": True}


def intent_classify(state: NLPState) -> dict:
    """LLM call that reads raw_transcript and returns one of the 8 top
    level intents. Replace the body with a real Claude/LLM call."""
    # placeholder classification logic
    intent = state.get("intent") or "normal_conv"
    return {
        "intent": intent,
        "conversation_history": state.get("conversation_history", [])
        + [{"role": "user", "text": state["raw_transcript"]}],
    }


def route_intent(state: NLPState) -> str:
    return state["intent"]


# ---------------------------------------------------------------------------
# Branch: menu
# ---------------------------------------------------------------------------

def menu_handler(state: NLPState) -> dict:
    # menu_category is set by NLP; here we just format a response
    category = state.get("menu_category", "all")
    return {"response_text": f"Showing menu: {category}"}


# ---------------------------------------------------------------------------
# Branch: call_people
# ---------------------------------------------------------------------------

def call_people_handler(state: NLPState) -> dict:
    target = state.get("call_target", "waiter")
    return {
        "robot_action": {"type": "call_person", "target": target},
        "response_text": f"Calling the {target} now.",
    }


# ---------------------------------------------------------------------------
# Branch: take_order (deepest sub-flow)
# ---------------------------------------------------------------------------

def route_order_action(state: NLPState) -> str:
    return state.get("order_action", "new_order")


def new_order_start(state: NLPState) -> dict:
    return {"order_slot": "ask_item"}


def ask_item(state: NLPState) -> dict:
    return {"order_slot": "ask_qty", "response_text": "What item would you like?"}


def ask_qty(state: NLPState) -> dict:
    return {"order_slot": "ask_preference", "response_text": "How many would you like?"}


def ask_preference(state: NLPState) -> dict:
    return {"order_slot": "ask_to_add", "response_text": "Any preference, e.g. spice level?"}


def ask_to_add(state: NLPState) -> dict:
    return {"awaiting_yes_no": "add_more", "response_text": "Would you like to add anything else?"}


def route_add_more(state: NLPState) -> str:
    # "yes" -> loop back to ask_item, "no" -> confirm_order
    return "ask_item" if state.get("awaiting_yes_no") == "add_more" and state.get(
        "order_slot"
    ) == "yes" else "confirm_order"


def confirm_order(state: NLPState) -> dict:
    return {"awaiting_yes_no": "confirm_order", "response_text": "Shall I confirm the order?"}


def route_confirm(state: NLPState) -> str:
    return "place_order" if state.get("order_confirmed") else "cancel_flow"


def place_order(state: NLPState) -> dict:
    return {"response_text": "Order confirmed and sent to the kitchen."}


def edit_order_start(state: NLPState) -> dict:
    return {"order_slot": "ask_item", "response_text": "Which item do you want to edit?"}


def route_edit_action(state: NLPState) -> str:
    return state.get("edit_action", "replace")


def edit_replace(state: NLPState) -> dict:
    return {"response_text": "Item replaced."}


def edit_customize(state: NLPState) -> dict:
    return {"response_text": "Item customized."}


def edit_cancel_single(state: NLPState) -> dict:
    return {"response_text": "That item was removed from the order."}


def edit_cancel_full(state: NLPState) -> dict:
    return {"response_text": "The whole order was cancelled."}


def cancel_order_start(state: NLPState) -> dict:
    return {"response_text": "Which item should I delete?"}


def cancel_delete_ask_item(state: NLPState) -> dict:
    return {"response_text": "Got it, removing that item."}


def add_item_start(state: NLPState) -> dict:
    # re-enters the same ask_item chain used by new_order
    return {"order_slot": "ask_item", "response_text": "What would you like to add?"}


# ---------------------------------------------------------------------------
# Branch: navigation
# ---------------------------------------------------------------------------

def navigation_handler(state: NLPState) -> dict:
    return {"response_text": "Where should I go?"}


def route_location(state: NLPState) -> str:
    # "IF Location = Kitchen / Docker" condition from the flowchart
    return state.get("location", "table")


def nav_docker(state: NLPState) -> dict:
    return {"robot_action": {"type": "navigate", "target": "docker"}, "response_text": "Heading to the docking station."}


def nav_kitchen(state: NLPState) -> dict:
    return {"robot_action": {"type": "navigate", "target": "kitchen"}, "response_text": "Heading to the kitchen."}


def nav_table(state: NLPState) -> dict:
    table = state.get("table_no", "unknown")
    return {"robot_action": {"type": "navigate", "target": f"table_{table}"}, "response_text": f"Heading to table {table}."}


# ---------------------------------------------------------------------------
# Branch: cutlery
# ---------------------------------------------------------------------------

def cutlery_handler(state: NLPState) -> dict:
    items = state.get("cutlery_requested", [])
    return {
        "robot_action": {"type": "deliver_cutlery", "items": items},
        "response_text": f"Bringing: {', '.join(items) if items else 'cutlery'}.",
    }


# ---------------------------------------------------------------------------
# Branch: about
# ---------------------------------------------------------------------------

def about_handler(state: NLPState) -> dict:
    topic = state.get("about_topic", "other")
    answers = {
        "who_are_you": "I'm Sonic, your restaurant service robot.",
        "what_can_you_do": "I can take orders, deliver cutlery, call staff, and guide you around.",
        "about_robot": "I'm powered by an LLM-based conversational system.",
        "famous_for": "I'm known for fast, friendly table service.",
        "other": "Happy to answer any other question about me!",
    }
    return {"response_text": answers.get(topic, answers["other"])}


# ---------------------------------------------------------------------------
# Branch: normal_conv / get_bill
# ---------------------------------------------------------------------------

def normal_conv_handler(state: NLPState) -> dict:
    return {"response_text": "Open-ended chat handled by the LLM."}


def get_bill_handler(state: NLPState) -> dict:
    total = state.get("bill_total", 0.0)
    return {"response_text": f"Your bill total is {total}."}


# ---------------------------------------------------------------------------
# Final shared response node
# ---------------------------------------------------------------------------

def respond(state: NLPState) -> dict:
    history = state.get("conversation_history", [])
    text = state.get("response_text", "")
    return {"conversation_history": history + [{"role": "robot", "text": text}]}


# ---------------------------------------------------------------------------
# Graph assembly
# ---------------------------------------------------------------------------

def build_graph():
    g = StateGraph(NLPState)

    # entry
    g.add_node("wake_word_listener", wake_word_listener)
    g.add_node("intent_classify", intent_classify)

    # top-level branch handlers
    g.add_node("menu_handler", menu_handler)
    g.add_node("call_people_handler", call_people_handler)
    g.add_node("navigation_handler", navigation_handler)
    g.add_node("cutlery_handler", cutlery_handler)
    g.add_node("about_handler", about_handler)
    g.add_node("normal_conv_handler", normal_conv_handler)
    g.add_node("get_bill_handler", get_bill_handler)

    # take_order sub-flow
    g.add_node("new_order_start", new_order_start)
    g.add_node("ask_item", ask_item)
    g.add_node("ask_qty", ask_qty)
    g.add_node("ask_preference", ask_preference)
    g.add_node("ask_to_add", ask_to_add)
    g.add_node("confirm_order", confirm_order)
    g.add_node("place_order", place_order)

    g.add_node("edit_order_start", edit_order_start)
    g.add_node("edit_replace", edit_replace)
    g.add_node("edit_customize", edit_customize)
    g.add_node("edit_cancel_single", edit_cancel_single)
    g.add_node("edit_cancel_full", edit_cancel_full)

    g.add_node("cancel_order_start", cancel_order_start)
    g.add_node("cancel_delete_ask_item", cancel_delete_ask_item)

    g.add_node("add_item_start", add_item_start)

    # navigation sub-flow
    g.add_node("nav_docker", nav_docker)
    g.add_node("nav_kitchen", nav_kitchen)
    g.add_node("nav_table", nav_table)

    # shared exit
    g.add_node("respond", respond)

    # wiring
    g.set_entry_point("wake_word_listener")
    g.add_edge("wake_word_listener", "intent_classify")

    g.add_conditional_edges(
        "intent_classify",
        route_intent,
        {
            "menu": "menu_handler",
            "call_people": "call_people_handler",
            "take_order": "new_order_start",  # default entry; see route_order_action below
            "navigation": "navigation_handler",
            "cutlery": "cutlery_handler",
            "about": "about_handler",
            "normal_conv": "normal_conv_handler",
            "get_bill": "get_bill_handler",
        },
    )

    # take_order: route by order_action (new / edit / cancel / add_item)
    g.add_conditional_edges(
        "new_order_start",
        route_order_action,
        {
            "new_order": "ask_item",
            "edit_order": "edit_order_start",
            "cancel_order": "cancel_order_start",
            "add_item": "add_item_start",
        },
    )

    # new order chain
    g.add_edge("ask_item", "ask_qty")
    g.add_edge("ask_qty", "ask_preference")
    g.add_edge("ask_preference", "ask_to_add")
    g.add_conditional_edges(
        "ask_to_add",
        route_add_more,
        {"ask_item": "ask_item", "confirm_order": "confirm_order"},  # yes -> loop, no -> confirm
    )
    g.add_conditional_edges(
        "confirm_order",
        route_confirm,
        {"place_order": "place_order", "cancel_flow": "respond"},
    )
    g.add_edge("place_order", "respond")

    # add_item re-enters the same ask_item chain
    g.add_edge("add_item_start", "ask_item")

    # edit order chain
    g.add_conditional_edges(
        "edit_order_start",
        route_edit_action,
        {
            "replace": "edit_replace",
            "customize": "edit_customize",
            "cancel_single": "edit_cancel_single",
            "cancel_full": "edit_cancel_full",
        },
    )
    for n in ["edit_replace", "edit_customize", "edit_cancel_single", "edit_cancel_full"]:
        g.add_edge(n, "respond")

    # cancel order chain
    g.add_edge("cancel_order_start", "cancel_delete_ask_item")
    g.add_edge("cancel_delete_ask_item", "respond")

    # navigation: conditional on location (Kitchen / Docker / Table)
    g.add_conditional_edges(
        "navigation_handler",
        route_location,
        {"docker": "nav_docker", "kitchen": "nav_kitchen", "table": "nav_table"},
    )
    for n in ["nav_docker", "nav_kitchen", "nav_table"]:
        g.add_edge(n, "respond")

    # simple branches straight to respond
    for n in [
        "menu_handler",
        "call_people_handler",
        "cutlery_handler",
        "about_handler",
        "normal_conv_handler",
        "get_bill_handler",
    ]:
        g.add_edge(n, "respond")

    g.add_edge("respond", END)

    return g.compile()


if __name__ == "__main__":
    app = build_graph()

    # example run: taking a new order
    result = app.invoke(
        {
            "raw_transcript": "I'd like to order a burger",
            "wake_word_detected": True,
            "intent": "take_order",
            "order_action": "new_order",
            "conversation_history": [],
        }
    )
    print(result["response_text"])
    print(result["conversation_history"])
