"""
dispatcher.py
-------------
Simulated physical action dispatcher.

Every action now runs a realistic sequence with proper timing:
  - Navigation: simulate travel time with progress updates
  - Food orders: go to kitchen, hand off to chef, return
  - Deliveries: go to source, pick up, go to destination, arrive
  - Notifications: send alert, wait for acknowledgement

On real Jetson + ROS2, replace the _navigate() and _travel() calls
with rclpy action clients to Nav2, and replace _notify_staff() with
ROS2 service calls or MQTT messages to staff devices.
The function signatures and return values stay exactly the same.
"""

import time
import threading

# ── TTS reference (set by main.py at startup) ─────────────────────────────────
# We import speak here so the robot can narrate what it's doing mid-action.
# e.g. "Heading to the kitchen now." → [travel] → "Order confirmed with the chef!"
try:
    from tts import speak as _speak
except ImportError:
    def _speak(text, **kwargs):
        print(f"  🔊 [TTS fallback] {text}")


# ── Robot state ───────────────────────────────────────────────────────────────
_state = {
    "battery_hours": 4.5,
    "current_task":  "idle",
    "volume":        70,
    "lights":        "off",
    "fan":           "off",
    "ac":            "off",
    "ac_temp":       24,
    "tv":            "off",
    "following":     False,
    "last_position": "base",
    "dnd_rooms":     set(),
}

_order_queue   = []   # items in the current food order
_last_order    = []   # copy of last confirmed order (for ROUND_AGAIN)

_specials_today = [
    "Grilled Salmon with Lemon Butter",
    "Mushroom Risotto",
    "Chocolate Lava Cake",
]
_tap_beers = [
    "Kingfisher", "Heineken", "Bira 91 White",
    "Corona", "Hoegaarden", "Tuborg",
]


# ── Low-level primitives ──────────────────────────────────────────────────────

def _log(action: str, detail: str = ""):
    print(f"  [ACTION: {action}] {detail}")


def _navigate(destination: str, travel_seconds: float = 3.0):
    """
    Simulate the robot driving to a destination.
    On ROS2 → send Nav2 goal and wait for result.
    """
    _state["current_task"] = f"navigating to {destination}"
    _log("NAVIGATE", f"Plotting route → {destination}")

    steps = max(1, int(travel_seconds / 1.0))
    for i in range(steps):
        time.sleep(1.0)
        progress = int(((i + 1) / steps) * 100)
        print(f"  ... navigating to {destination}: {progress}%")

    _state["last_position"] = destination
    _state["current_task"]  = "idle"
    _log("NAVIGATE", f"Arrived at {destination} ✓")


def _notify_staff(role: str, message: str):
    """
    Simulate sending an alert to a staff member's device.
    On ROS2 → publish to /sonic/staff_alert topic or MQTT.
    """
    _log("NOTIFY", f"→ {role}: {message}")
    time.sleep(0.5)   # simulate network send
    print(f"  ... {role} has been notified ✓")


# ── General ───────────────────────────────────────────────────────────────────

def enable_listening():
    _log("ENABLE_LISTENING", "Microphone activated.")
    _state["current_task"] = "listening"
    return "listening_active"

def disable_listening():
    _log("DISABLE_LISTENING", "Entering standby.")
    _state["current_task"] = "standby"
    return "standby"

def speak_identity():
    _log("SPEAK_IDENTITY")
    return "identity_spoken"

def get_battery_level() -> str:
    hours = _state["battery_hours"]
    _log("GET_BATTERY", f"{hours:.1f} hours remaining.")
    return f"{hours:.1f}"

def get_current_task() -> str:
    task = _state["current_task"]
    _log("GET_TASK", f"Current task: {task}")
    return task

def speak_health_status():
    _log("HEALTH_STATUS", "All systems nominal.")
    return "healthy"

def go_to_dock():
    _log("GO_TO_DOCK", "Heading to charging dock...")
    _navigate("charging dock", travel_seconds=4.0)
    _state["current_task"] = "charging"
    return "at_dock"

def go_to_base():
    _log("GO_TO_BASE", "Returning to base station...")
    _navigate("base station", travel_seconds=4.0)
    return "at_base"

def follow_person():
    _log("FOLLOW_PERSON", "Person-following mode enabled.")
    _state["following"] = True
    _state["current_task"] = "following_person"
    return "following"

def stop_following():
    _log("STOP_FOLLOWING", "Holding position.")
    _state["following"] = False
    _state["current_task"] = "idle"
    return "stopped"

def navigate_to(place: str):
    _navigate(place or "requested location", travel_seconds=3.0)
    return f"arrived_at_{place}"

def repeat_last_tts(last_utterance: str):
    _log("REPEAT_TTS", f"Replaying: \"{last_utterance}\"")
    return "repeated"

def cancel_current_task():
    old = _state["current_task"]
    _log("CANCEL_TASK", f"Cancelling: {old}")
    _state["current_task"] = "idle"
    return "cancelled"

def speak_capabilities():
    _log("SPEAK_CAPABILITIES")
    return "capabilities_spoken"

def increase_volume():
    _state["volume"] = min(100, _state["volume"] + 15)
    _log("VOLUME_UP", f"Volume → {_state['volume']}%")
    return str(_state["volume"])

def decrease_volume():
    _state["volume"] = max(0, _state["volume"] - 15)
    _log("VOLUME_DOWN", f"Volume → {_state['volume']}%")
    return str(_state["volume"])


# ── Restaurant — full physical sequences ─────────────────────────────────────

def take_order(item: str = ""):
    """
    SEQUENCE:
    1. Record order at the table
    2. Navigate to the kitchen
    3. Hand off order to chef / kitchen staff
    4. Navigate back to the customer's table
    """
    global _last_order
    _log("TAKE_ORDER", f"Recording order at table: {item or 'items noted'}")
    if item:
        _order_queue.append(item)

    _speak("Got it! Let me take this to the kitchen right now.")
    time.sleep(0.5)

    # go to kitchen
    _navigate("kitchen", travel_seconds=3.0)

    # hand off to chef
    order_summary = ", ".join(_order_queue) if _order_queue else item or "order"
    _notify_staff("Chef", f"New order: {order_summary}")
    _speak("Order confirmed with the kitchen!")
    time.sleep(0.5)

    # copy to last_order for repeat functionality
    _last_order = list(_order_queue)

    # return to floor / customer area
    _navigate("customer area", travel_seconds=3.0)

    _state["current_task"] = "idle"
    return "order_placed_with_kitchen"


def bring_menu():
    """Navigate to menu storage, pick it up, bring it to the table."""
    _log("BRING_MENU", "Collecting menu...")
    _navigate("menu station", travel_seconds=2.0)
    _speak("Got the menu! Bringing it right over.")
    _navigate("customer table", travel_seconds=2.0)
    return "menu_delivered"


def speak_specials() -> str:
    specials = ", ".join(_specials_today)
    _log("SPEAK_SPECIALS", f"Today: {specials}")
    return specials


def add_to_order(item: str = ""):
    _log("ADD_TO_ORDER", f"Adding: {item}")
    if item:
        _order_queue.append(item)

    _speak("Adding that to your order — let me update the kitchen.")
    time.sleep(0.5)
    _navigate("kitchen", travel_seconds=3.0)
    _notify_staff("Chef", f"Update: add {item or 'extra item'} to current order")
    _navigate("customer area", travel_seconds=3.0)
    return "order_updated"


def cancel_order():
    _log("CANCEL_ORDER", "Notifying kitchen of cancellation.")
    _navigate("kitchen", travel_seconds=3.0)
    _notify_staff("Chef", "CANCEL the current order immediately")
    _order_queue.clear()
    _navigate("customer area", travel_seconds=3.0)
    return "order_cancelled"


def filter_menu(filter_type: str = "vegetarian"):
    _log("FILTER_MENU", f"Filter: {filter_type}")
    return "menu_filtered"


def query_dish_ingredients(dish: str = ""):
    _log("QUERY_INGREDIENTS", f"Looking up: {dish or 'selected dish'}")
    time.sleep(1.0)   # simulate DB lookup
    return "ingredients_found"


def bring_bill():
    """Go to billing counter, get the bill, bring it to the table."""
    _log("BRING_BILL", "Generating bill...")
    _navigate("billing counter", travel_seconds=2.0)
    time.sleep(1.0)   # simulate printing/generating
    _speak("Got the bill! Bringing it to your table now.")
    _navigate("customer table", travel_seconds=2.0)
    return "bill_delivered"


def notify_waiter():
    _notify_staff("Waiter", "Customer is requesting your assistance — please come to the table.")
    return "waiter_notified"


def call_manager():
    _notify_staff("Manager", "Customer is requesting to speak with the manager.")
    return "manager_called"


def notify_chef_urgency():
    _navigate("kitchen", travel_seconds=3.0)
    _notify_staff("Chef", "⚠️ URGENT — Customer has been waiting too long. Please prioritise this order.")
    _navigate("customer area", travel_seconds=3.0)
    return "chef_notified_urgent"


def speak_hours():
    _log("SPEAK_HOURS", "Open 11 PM, kitchen closes 10:30 PM.")
    return "hours_spoken"


def speak_directions(location: str = "restroom"):
    _log("SPEAK_DIRECTIONS", f"Giving directions to: {location}")
    return "directions_given"


def deliver_water():
    """Go to water station, pick up water, deliver to table."""
    _log("DELIVER_WATER", "Picking up water...")
    _navigate("water station", travel_seconds=2.0)
    _speak("Got it! Bringing the water to your table.")
    _navigate("customer table", travel_seconds=2.0)
    return "water_delivered"


def notify_manager(reason: str = "complaint"):
    _notify_staff("Manager", f"Customer complaint: {reason}. Please attend immediately.")
    return "manager_notified"


def speak_goodbye():
    _log("SPEAK_GOODBYE")
    return "goodbye_spoken"


def speak_payment_methods():
    _log("SPEAK_PAYMENT", "UPI, Cards, Cash.")
    return "payment_spoken"


# ── Hotel — full physical sequences ──────────────────────────────────────────

def check_reservation(name: str = ""):
    _log("CHECK_RESERVATION", f"Looking up: {name or 'guest'}")
    time.sleep(1.5)   # simulate system lookup
    return "reservation_found"

def notify_manager_checkout():
    _notify_staff("Front Desk Manager", "Guest checkout requested.")
    return "checkout_initiated"

def check_late_checkout():
    time.sleep(1.0)
    return "available"

def take_luggage():
    _log("TAKE_LUGGAGE", "Picking up luggage from guest.")
    time.sleep(1.0)
    return "luggage_stored"

def room_delivery(room: str = "101", item: str = "food"):
    _log("ROOM_DELIVERY", f"Delivering {item} to room {room}")
    _navigate(f"kitchen/pickup area", travel_seconds=3.0)
    _speak(f"Got your {item}! Heading to room {room} now.")
    _navigate(f"room {room}", travel_seconds=4.0)
    return "delivered"

def notify_housekeeping(room: str = ""):
    _notify_staff("Housekeeping", f"Please clean room {room or 'requested room'} as soon as possible.")
    return "housekeeping_scheduled"

def set_dnd(room: str = ""):
    _log("SET_DND", f"DND for room: {room or 'current room'}")
    if room:
        _state["dnd_rooms"].add(room)
    _notify_staff("Front Desk", f"Do Not Disturb set for room {room}. No service until cancelled.")
    return "dnd_set"

def deliver_towels():
    _navigate("linen room", travel_seconds=2.0)
    _speak("Picked up the towels! On my way to your room.")
    _navigate("guest room", travel_seconds=3.0)
    return "towels_delivered"

def deliver_toiletries():
    _navigate("housekeeping store", travel_seconds=2.0)
    _speak("Got the toiletries! Heading to your room now.")
    _navigate("guest room", travel_seconds=3.0)
    return "toiletries_delivered"

def notify_maintenance(room: str = ""):
    _notify_staff("Maintenance", f"Issue reported in room {room or 'guest room'}. Please attend urgently.")
    return "maintenance_notified"

def deliver_linen():
    _navigate("linen room", travel_seconds=2.0)
    _speak("Got the fresh linen! Heading to your room.")
    _navigate("guest room", travel_seconds=3.0)
    return "linen_delivered"

def navigate_to_room(room: str = ""):
    _navigate(f"room {room}" if room else "guest room", travel_seconds=4.0)
    return "at_room"

def navigate_to_restaurant():
    _navigate("hotel restaurant", travel_seconds=4.0)
    return "at_restaurant"

def navigate_to_gym():
    _navigate("fitness centre", travel_seconds=4.0)
    return "at_gym"

def navigate_to_elevator():
    _navigate("elevator", travel_seconds=3.0)
    return "at_elevator"

def navigate_to_reception():
    _navigate("reception / front desk", travel_seconds=3.0)
    return "at_reception"

def navigate_to_smoking_area():
    _navigate("outdoor smoking area", travel_seconds=4.0)
    return "at_smoking_area"

def alert_emergency():
    _log("EMERGENCY", "⚠️  MEDICAL EMERGENCY — alerting all staff!")
    _notify_staff("Security + Front Desk + Manager", "⚠️ MEDICAL EMERGENCY — send help immediately!")
    return "emergency_alerted"

def notify_staff(reason: str = ""):
    _notify_staff("Front Desk", reason or "Guest needs assistance.")
    return "staff_notified"

def alert_security(description: str = ""):
    _log("SECURITY_ALERT", f"⚠️  LOST CHILD — {description}")
    _notify_staff("All Staff + Security", f"⚠️ LOST CHILD: {description or 'no description yet'}. Search immediately!")
    return "security_alerted"


# ── Bar — full physical sequences ─────────────────────────────────────────────

def notify_bartender(order: str = ""):
    _notify_staff("Bartender", f"Order: {order or 'customer order'}")
    _navigate("bar counter", travel_seconds=2.0)
    _speak("Picked up your drink! Bringing it right over.")
    _navigate("customer", travel_seconds=2.0)
    return "drink_delivered"

def repeat_order():
    if _last_order:
        summary = ", ".join(_last_order)
        _notify_staff("Bartender", f"REPEAT ORDER: {summary}")
        _navigate("bar counter", travel_seconds=2.0)
        _speak("Got your repeat order! Coming right up.")
        _navigate("customer", travel_seconds=2.0)
    else:
        _log("REPEAT_ORDER", "No previous order on record.")
    return "order_repeated"

def bring_drinks_menu():
    _navigate("bar counter", travel_seconds=2.0)
    _speak("Got the drinks menu! Bringing it to you.")
    _navigate("customer", travel_seconds=2.0)
    return "drinks_menu_delivered"

def speak_tap_list() -> str:
    beers = ", ".join(_tap_beers)
    _log("TAP_LIST", f"On tap: {beers}")
    return beers

def bring_nonalcoholic_menu():
    _navigate("bar counter", travel_seconds=2.0)
    _navigate("customer", travel_seconds=2.0)
    return "nonalcoholic_menu_delivered"

def bring_wine_menu():
    _navigate("bar counter", travel_seconds=2.0)
    _navigate("customer", travel_seconds=2.0)
    return "wine_menu_delivered"


# ── Home / Smart home ─────────────────────────────────────────────────────────

def send_command_lights_on():
    _state["lights"] = "on"
    _log("LIGHTS_ON", "Command sent to smart light controller.")
    return "lights_on"

def send_command_lights_off():
    _state["lights"] = "off"
    _log("LIGHTS_OFF", "Command sent to smart light controller.")
    return "lights_off"

def send_command_fan_on():
    _state["fan"] = "on"
    _log("FAN_ON", "Command sent to fan relay.")
    return "fan_on"

def send_command_fan_off():
    _state["fan"] = "off"
    _log("FAN_OFF", "Command sent to fan relay.")
    return "fan_off"

def send_command_ac_temp(temp: str = ""):
    if temp and temp.isdigit():
        _state["ac_temp"] = int(temp)
    _log("AC_TEMP", f"Setting AC to {_state['ac_temp']}°C.")
    return str(_state["ac_temp"])

def send_command_ac_off():
    _state["ac"] = "off"
    _log("AC_OFF", "Command sent to AC unit.")
    return "ac_off"

def send_command_tv_on():
    _state["tv"] = "on"
    _log("TV_ON", "Command sent to smart TV.")
    return "tv_on"

def send_command_tv_off():
    _state["tv"] = "off"
    _log("TV_OFF", "Command sent to smart TV.")
    return "tv_off"

def activate_sleep_mode():
    _log("SLEEP_MODE", "Activating sleep mode: dims lights, lowers AC, sets alarm.")
    send_command_lights_off()
    send_command_ac_temp("22")
    return "sleep_mode_active"

def activate_morning_mode():
    _log("MORNING_MODE", "Morning mode: brightening lights, checking weather.")
    send_command_lights_on()
    return "morning_mode_active"

def activate_away_mode():
    _log("AWAY_MODE", "Away mode: locking, lights off, AC off, arming security.")
    send_command_lights_off()
    send_command_ac_off()
    send_command_fan_off()
    return "away_mode_active"

def activate_home_mode():
    _log("HOME_MODE", "Home mode: lights on, AC to preferred temp.")
    send_command_lights_on()
    send_command_ac_temp("23")
    return "home_mode_active"

def query_presence_sensors():
    import random
    result = random.choice(["someone_home", "nobody_home"])
    _log("PRESENCE_SENSOR", f"Motion scan complete: {result}")
    return result


# ── Dispatcher router ─────────────────────────────────────────────────────────

def dispatch(intent: str, slots: dict, context) -> str:
    """
    Route an intent to its physical action function.
    All navigation and delivery actions now simulate real travel time
    and speak mid-action narration via TTS.
    """
    p    = slots.get("place", "")
    r    = slots.get("room", "")
    item = slots.get("item", "")
    temp = slots.get("temperature", "")

    action_map = {
        # General
        "WAKE_UP":             lambda: enable_listening(),
        "SLEEP":               lambda: disable_listening(),
        "IDENTIFY":            lambda: speak_identity(),
        "BATTERY_STATUS":      lambda: get_battery_level(),
        "TASK_STATUS":         lambda: get_current_task(),
        "HEALTH_CHECK":        lambda: speak_health_status(),
        "GO_CHARGE":           lambda: go_to_dock(),
        "GO_HOME":             lambda: go_to_base(),
        "FOLLOW":              lambda: follow_person(),
        "STOP_FOLLOW":         lambda: stop_following(),
        "NAVIGATE_TO":         lambda: navigate_to(p or "requested location"),
        "REPEAT":              lambda: repeat_last_tts(context.last_bot_utterance),
        "CANCEL":              lambda: cancel_current_task(),
        "LIST_CAPABILITIES":   lambda: speak_capabilities(),
        "VOLUME_UP":           lambda: increase_volume(),
        "VOLUME_DOWN":         lambda: decrease_volume(),
        # Restaurant
        "TAKE_ORDER":          lambda: take_order(item),
        "SHOW_MENU":           lambda: bring_menu(),
        "ASK_SPECIALS":        lambda: speak_specials(),
        "ADD_TO_ORDER":        lambda: add_to_order(item),
        "CANCEL_ORDER":        lambda: cancel_order(),
        "FILTER_MENU":         lambda: filter_menu(item),
        "DISH_INGREDIENTS":    lambda: query_dish_ingredients(item),
        "REQUEST_BILL":        lambda: bring_bill(),
        "BILL_DISPUTE":        lambda: call_manager(),
        "PAYMENT_METHOD":      lambda: speak_payment_methods(),
        "CALL_WAITER":         lambda: notify_waiter(),
        "CALL_MANAGER":        lambda: call_manager(),
        "ORDER_DELAY":         lambda: notify_chef_urgency(),
        "CLOSING_TIME":        lambda: speak_hours(),
        "FIND_RESTROOM":       lambda: speak_directions("restroom"),
        "REQUEST_WATER":       lambda: deliver_water(),
        "COLD_FOOD_COMPLAINT": lambda: notify_waiter(),
        "WRONG_ORDER":         lambda: notify_manager("wrong order delivered"),
        "LEAVING_RESTAURANT":  lambda: speak_goodbye(),
        # Hotel
        "CHECK_IN":            lambda: check_reservation(),
        "CHECK_OUT":           lambda: notify_manager_checkout(),
        "LATE_CHECKOUT":       lambda: check_late_checkout(),
        "LUGGAGE_STORAGE":     lambda: take_luggage(),
        "ROOM_DELIVERY":       lambda: room_delivery(r, item),
        "HOUSEKEEPING":        lambda: notify_housekeeping(r),
        "DO_NOT_DISTURB":      lambda: set_dnd(r),
        "EXTRA_TOWELS":        lambda: deliver_towels(),
        "REQUEST_TOILETRIES":  lambda: deliver_toiletries(),
        "MAINTENANCE":         lambda: notify_maintenance(r),
        "CHANGE_LINEN":        lambda: deliver_linen(),
        "GUIDE_TO_ROOM":       lambda: navigate_to_room(r),
        "FIND_RESTAURANT":     lambda: navigate_to_restaurant(),
        "FIND_GYM":            lambda: navigate_to_gym(),
        "FIND_ELEVATOR":       lambda: navigate_to_elevator(),
        "FIND_RECEPTION":      lambda: navigate_to_reception(),
        "FIND_SMOKING_AREA":   lambda: navigate_to_smoking_area(),
        "MEDICAL_EMERGENCY":   lambda: alert_emergency(),
        "LOCKED_OUT":          lambda: notify_staff("guest locked out of room"),
        "LOST_CHILD":          lambda: alert_security(),
        # Bar
        "ORDER_BEER":          lambda: notify_bartender("beer"),
        "ORDER_COCKTAIL":      lambda: notify_bartender(item or "cocktail"),
        "ROUND_AGAIN":         lambda: repeat_order(),
        "SHOW_DRINKS_MENU":    lambda: bring_drinks_menu(),
        "WHAT_ON_TAP":         lambda: speak_tap_list(),
        "NON_ALCOHOLIC":       lambda: bring_nonalcoholic_menu(),
        "BARTENDER_CHOICE":    lambda: notify_bartender("bartender special"),
        "ORDER_WATER_BAR":     lambda: deliver_water(),
        "ORDER_WINE":          lambda: bring_wine_menu(),
        "ORDER_SHOTS":         lambda: notify_bartender(item or "shots"),
        # Home
        "NAVIGATE_ROOM":       lambda: navigate_to(p or "requested room"),
        "LIGHTS_ON":           lambda: send_command_lights_on(),
        "LIGHTS_OFF":          lambda: send_command_lights_off(),
        "FAN_ON":              lambda: send_command_fan_on(),
        "FAN_OFF":             lambda: send_command_fan_off(),
        "SET_TEMPERATURE":     lambda: send_command_ac_temp(temp),
        "AC_OFF":              lambda: send_command_ac_off(),
        "TV_ON":               lambda: send_command_tv_on(),
        "TV_OFF":              lambda: send_command_tv_off(),
        "GOODNIGHT_MODE":      lambda: activate_sleep_mode(),
        "GOOD_MORNING":        lambda: activate_morning_mode(),
        "LEAVING_HOME":        lambda: activate_away_mode(),
        "ARRIVING_HOME":       lambda: activate_home_mode(),
        "WHO_IS_HOME":         lambda: query_presence_sensors(),
    }

    fn = action_map.get(intent)
    if fn:
        try:
            return str(fn())
        except Exception as e:
            print(f"  [Dispatcher] Error in {intent}: {e}")
            return "error"
    else:
        print(f"  [Dispatcher] No action mapped for: {intent}")
        return "unhandled"


def get_state() -> dict:
    return {k: (list(v) if isinstance(v, set) else v) for k, v in _state.items()}

def reset_state():
    global _order_queue, _last_order
    _state.update({
        "battery_hours": 4.5, "current_task": "idle", "volume": 70,
        "lights": "off", "fan": "off", "ac": "off", "ac_temp": 24,
        "tv": "off", "following": False, "last_position": "base", "dnd_rooms": set(),
    })
    _order_queue.clear()
    _last_order.clear()

# legacy alias
def get_robot_state():
    return get_state()
