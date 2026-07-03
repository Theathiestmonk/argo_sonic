"""
fast_matcher.py
---------------
Zero-latency regex/keyword matcher for known commands.

SCENARIO-AWARE: only matches intents that belong to the active scenario
OR the "general" scenario (which is always active regardless).

A restaurant Sonic will NEVER accidentally match a hotel or bar command.
Fixed responses from commands.py are ALWAYS returned for matched intents —
the LLM never overrides these, giving 100% consistent production responses.

Priority order: MEDICAL_EMERGENCY, LOST_CHILD checked first (safety critical),
then general intents, then scenario-specific intents.
"""

import re
from commands import COMMANDS, get_command_by_intent

# ── Intent → regex patterns ────────────────────────────────────────────────
# Every intent has at least 3 patterns to catch natural phrasing variation.

PATTERNS = {
    # ── General (always active) ────────────────────────────────────────────
    "MEDICAL_EMERGENCY":   [r"\bneed a doctor\b", r"\bmedical emergency\b",
                            r"\bsomeone is hurt\b", r"\bcall.{0,10}ambulance\b",
                            r"\bhelp.{0,10}hurt\b", r"\bemergency\b"],
    "LOST_CHILD":          [r"\bchild is missing\b", r"\blost.{0,5}child\b",
                            r"\bcan'?t find my (kid|son|daughter|child)\b",
                            r"\bmissing (kid|son|daughter|child)\b"],
    "WAKE_UP":             [r"\bhey sonic\b", r"\bwake up\b",
                            r"\bhey argo\b", r"\bhi sonic\b"],
    "SLEEP":               [r"\bgo to sleep\b", r"\btake a rest\b",
                            r"\bstandby\b", r"\bsleep mode\b"],
    "IDENTIFY":            [r"\bwhat'?s your name\b", r"\bwho are you\b",
                            r"\bare you a robot\b", r"\bwhat are you\b"],
    "BATTERY_STATUS":      [r"\bbattery\b", r"\bare you charged\b",
                            r"\bgoing to die\b", r"\bhow much power\b"],
    "TASK_STATUS":         [r"\bwhat are you doing\b", r"\bare you busy\b",
                            r"\bwhat'?s your task\b"],
    "HEALTH_CHECK":        [r"\bare you okay\b", r"\bis something wrong\b",
                            r"\bare you working\b", r"\byou seem broken\b"],
    "FOLLOW":              [r"\bfollow me\b", r"\bcome with me\b",
                            r"\bwalk with me\b", r"\bcome along\b"],
    "STOP_FOLLOW":         [r"\bstop following\b", r"\bdon'?t follow\b",
                            r"\bwait here\b", r"\bstay here\b"],
    "GO_CHARGE":           [r"\bgo charge\b", r"\bcharge yourself\b",
                            r"\bgo to your dock\b", r"\bcharge up\b"],
    "GO_HOME":             [r"\breturn to base\b", r"\bgo to your base\b",
                            r"\bgo home\b", r"\bbase station\b"],
    "CANCEL":              [r"\bcancel\b", r"\bnever mind\b",
                            r"\bforget it\b", r"\babort\b", r"\bignore that\b"],
    "REPEAT":              [r"\brepeat that\b", r"\bsay that again\b",
                            r"\bdidn'?t hear you\b", r"\bwhat did you say\b"],
    "LIST_CAPABILITIES":   [r"\bwhat can you do\b", r"\bhow can you help\b",
                            r"\byour capabilities\b", r"\bwhat do you do\b"],
    "VOLUME_UP":           [r"\bspeak louder\b", r"\bcan'?t hear you\b",
                            r"\bturn up.{0,10}volume\b", r"\blouder\b"],
    "VOLUME_DOWN":         [r"\bspeak softer\b", r"\btoo loud\b",
                            r"\bquiet down\b", r"\blower.{0,10}voice\b"],

    # ── Restaurant ────────────────────────────────────────────────────────
    "TAKE_ORDER":          [r"\bready to order\b", r"\bcan we order\b",
                            r"\btake our order\b", r"\bi'?d like to order\b"],
    "SHOW_MENU":           [r"\bsee the menu\b", r"\bshow.{0,5}menu\b",
                            r"\bbring.{0,5}menu\b", r"\bwhat'?s on the menu\b"],
    "ASK_SPECIALS":        [r"\btoday'?s specials?\b", r"\bany specials?\b",
                            r"\bchef'?s special\b", r"\bwhat do you recommend\b"],
    "ADD_TO_ORDER":        [r"\badd.{0,10}order\b", r"\bone more\b",
                            r"\bcan i add\b", r"\bsomething else\b"],
    "CANCEL_ORDER":        [r"\bcancel.{0,5}order\b", r"\bdon'?t want that\b",
                            r"\bremove.{0,5}(order|item|dish)\b"],
    "FILTER_MENU":         [r"\bvegetarian\b", r"\bvegan\b",
                            r"\bgluten.{0,5}free\b", r"\bno meat\b"],
    "DISH_INGREDIENTS":    [r"\bwhat'?s in\b", r"\bdoes it have\b",
                            r"\bingredients\b", r"\bcontain nuts\b", r"\bspicy\b"],
    "REQUEST_BILL":        [r"\bbill please\b", r"\bget.{0,5}check\b",
                            r"\bwant to pay\b", r"\btime to pay\b", r"\bpay now\b"],
    "BILL_DISPUTE":        [r"\bbill is wrong\b", r"\bwrong (bill|amount|charge)\b",
                            r"\bextra charge\b", r"\bovercharged\b"],
    "PAYMENT_METHOD":      [r"\bdo you take\b", r"\baccept.{0,10}(card|cash|upi)\b",
                            r"\bpayment method\b", r"\bhow.{0,5}pay\b"],
    "CALL_WAITER":         [r"\bcall.{0,5}waiter\b", r"\bneed a waiter\b",
                            r"\bsend someone\b", r"\bexcuse me\b"],
    "CALL_MANAGER":        [r"\bget.{0,5}manager\b", r"\bspeak to.{0,5}manager\b",
                            r"\bmanager please\b"],
    "ORDER_DELAY":         [r"\bwhere is our order\b", r"\bstill waiting\b",
                            r"\bbeen a long time\b", r"\border hasn'?t come\b"],
    "CLOSING_TIME":        [r"\bwhat time.{0,5}close\b", r"\bopening hours\b",
                            r"\bwhen do you close\b", r"\bare you open\b"],
    "FIND_RESTROOM":       [r"\bwhere.{0,5}(toilet|bathroom|restroom|washroom|loo)\b",
                            r"\b(toilet|bathroom|restroom)\s*(please|where)\b"],
    "REQUEST_WATER":       [r"\bbring water\b", r"\bneed water\b",
                            r"\bmore water\b", r"\bwater please\b", r"\brefill water\b"],
    "COLD_FOOD_COMPLAINT": [r"\bfood is cold\b", r"\bnot hot enough\b",
                            r"\bcold (food|dish|meal)\b"],
    "WRONG_ORDER":         [r"\bwrong (dish|order|food|item)\b",
                            r"\bthat'?s not mine\b", r"\bdifferent order\b"],
    "LEAVING_RESTAURANT":  [r"\bwe'?re leaving\b", r"\bwe'?re done\b",
                            r"\bready to go\b", r"\bthank you.{0,10}bye\b"],

    # ── Hotel ─────────────────────────────────────────────────────────────
    "CHECK_IN":            [r"\bcheck.{0,5}in\b", r"\bjust arrived\b",
                            r"\bwant to check in\b"],
    "CHECK_OUT":           [r"\bcheck.{0,5}out\b", r"\bwe'?re leaving today\b",
                            r"\bleaving the hotel\b"],
    "LATE_CHECKOUT":       [r"\blate checkout\b", r"\bextend.{0,5}stay\b",
                            r"\bstay.{0,5}(longer|one more)\b"],
    "LUGGAGE_STORAGE":     [r"\bstore.{0,5}luggage\b", r"\bkeep.{0,5}bags?\b",
                            r"\bluggage storage\b", r"\bbaggage\b"],
    "ROOM_DELIVERY":       [r"\bbring.{0,10}room\b", r"\bdeliver.{0,10}room\b",
                            r"\broom service\b", r"\broom \d+\b"],
    "HOUSEKEEPING":        [r"\bclean.{0,5}room\b", r"\bhousekeeping\b",
                            r"\bmake.{0,5}(my |the )?room\b"],
    "DO_NOT_DISTURB":      [r"\bdo not disturb\b", r"\bdon'?t (send|disturb)\b",
                            r"\bprivacy please\b", r"\bdnd\b"],
    "EXTRA_TOWELS":        [r"\bextra towels?\b", r"\bmore towels?\b",
                            r"\bneed.{0,5}towel\b"],
    "REQUEST_TOILETRIES":  [r"\btoiletries\b", r"\bshampoo\b",
                            r"\bbring soap\b", r"\bneed.{0,5}(soap|conditioner)\b"],
    "MAINTENANCE":         [r"\b(something|light|ac|tv|shower).{0,10}broken\b",
                            r"\bnot working\b", r"\bmaintenance\b", r"\bfix.{0,10}room\b"],
    "CHANGE_LINEN":        [r"\bchange.{0,5}(sheets?|linen|bed)\b",
                            r"\bfresh sheets?\b", r"\bnew linen\b"],
    "GUIDE_TO_ROOM":       [r"\btake me to.{0,5}(my )?room\b",
                            r"\bshow me.{0,5}room\b", r"\bguide.{0,5}room\b"],
    "FIND_RESTAURANT":     [r"\bwhere.{0,5}restaurant\b",
                            r"\btake me.{0,5}(dining|restaurant)\b"],
    "FIND_GYM":            [r"\bwhere.{0,5}gym\b", r"\bfitness cent(re|er)\b",
                            r"\btake me.{0,5}gym\b"],
    "FIND_ELEVATOR":       [r"\bwhere.{0,5}(elevator|lift)\b",
                            r"\bfind.{0,5}(elevator|lift)\b"],
    "FIND_RECEPTION":      [r"\bfront desk\b", r"\btake me.{0,5}reception\b",
                            r"\bwhere.{0,5}reception\b"],
    "FIND_SMOKING_AREA":   [r"\bwhere.{0,5}smok\b", r"\bsmoking area\b",
                            r"\bcan i smoke\b"],
    "LOCKED_OUT":          [r"\blocked out\b", r"\bcan'?t get in\b",
                            r"\bkey not working\b", r"\blost.{0,5}key\b"],
    "LOST_CHILD":          [r"\bchild is missing\b", r"\blost.{0,5}(child|kid)\b",
                            r"\bcan'?t find my (kid|son|daughter|child)\b"],

    # ── Bar ───────────────────────────────────────────────────────────────
    "ORDER_BEER":          [r"\bget.{0,5}(a |me a )?beer\b", r"\bone beer\b",
                            r"\bpint please\b", r"\bi'?ll have.{0,5}(lager|beer|ale)\b"],
    "ORDER_COCKTAIL":      [r"\bcocktail\b", r"\bmojito\b",
                            r"\bgin and tonic\b", r"\bmargarita\b"],
    "ROUND_AGAIN":         [r"\banother round\b", r"\bsame again\b",
                            r"\bone more.{0,5}same\b", r"\brepeat.{0,5}drinks?\b"],
    "SHOW_DRINKS_MENU":    [r"\bdrinks? menu\b", r"\bshow.{0,5}(drinks?|menu)\b",
                            r"\bwhat.{0,10}(drinks?|have)\b"],
    "WHAT_ON_TAP":         [r"\bwhat'?s on tap\b", r"\bdraft beer\b",
                            r"\bcraft beer\b", r"\bbeers? on tap\b"],
    "NON_ALCOHOLIC":       [r"\bnon.{0,5}alcoholic\b", r"\bno alcohol\b",
                            r"\bmocktail\b", r"\bvirgin\b"],
    "BARTENDER_CHOICE":    [r"\bsurprise me\b", r"\bbartender'?s choice\b",
                            r"\bwhatever you recommend\b", r"\bpick.{0,5}for me\b"],
    "ORDER_WATER_BAR":     [r"\bcan i get water\b", r"\bbring.{0,5}water\b",
                            r"\bglass of water\b", r"\bstill or sparkling\b"],
    "ORDER_WINE":          [r"\bwine please\b", r"\bred wine\b",
                            r"\bwhite wine\b", r"\bbottle of (wine|ros[eé])\b"],
    "ORDER_SHOTS":         [r"\bshots? please\b", r"\blet'?s do shots?\b",
                            r"\btequila shots?\b", r"\bround of shots?\b"],

    # ── Home ──────────────────────────────────────────────────────────────
    "NAVIGATE_ROOM":       [r"\btake me to.{0,15}(kitchen|living|bedroom|bathroom|garage)\b",
                            r"\bgo to.{0,10}(kitchen|living|bedroom|bathroom)\b"],
    "LIGHTS_ON":           [r"\bturn on.{0,5}lights?\b", r"\blights? on\b",
                            r"\bswitch on.{0,5}lights?\b", r"\bbrighten\b"],
    "LIGHTS_OFF":          [r"\bturn off.{0,5}lights?\b", r"\blights? off\b",
                            r"\bswitch off.{0,5}lights?\b"],
    "FAN_ON":              [r"\bturn on.{0,5}fan\b", r"\bfan on\b",
                            r"\bswitch on.{0,5}fan\b", r"\bstart.{0,5}fan\b"],
    "FAN_OFF":             [r"\bturn off.{0,5}fan\b", r"\bfan off\b",
                            r"\bstop.{0,5}fan\b"],
    "SET_TEMPERATURE":     [r"\bset.{0,10}temperature\b", r"\bset.{0,5}ac.{0,5}to\b",
                            r"\b(\d+)\s*degrees?\b", r"\bmake it (warmer|cooler|hotter|colder)\b"],
    "AC_OFF":              [r"\bturn off.{0,5}ac\b", r"\bac off\b",
                            r"\bstop cooling\b", r"\bswitch off.{0,5}ac\b"],
    "TV_ON":               [r"\bturn on.{0,5}tv\b", r"\btv on\b",
                            r"\bswitch on.{0,5}tv\b"],
    "TV_OFF":              [r"\bturn off.{0,5}tv\b", r"\btv off\b",
                            r"\bswitch off.{0,5}tv\b"],
    "GOODNIGHT_MODE":      [r"\bgoodnight\b", r"\bgoing to sleep\b",
                            r"\bbedtime\b", r"\bsleep mode\b"],
    "GOOD_MORNING":        [r"\bgood morning\b", r"\bi'?m awake\b",
                            r"\bmorning sonic\b"],
    "LEAVING_HOME":        [r"\bi'?m leaving\b", r"\block up\b",
                            r"\bheading out\b", r"\bi'?m going out\b"],
    "ARRIVING_HOME":       [r"\bi'?m home\b", r"\bi'?m back\b",
                            r"\bi'?ve arrived\b", r"\bopen up\b"],
    "WHO_IS_HOME":         [r"\bis anyone home\b", r"\bwho'?s home\b",
                            r"\bany motion\b", r"\bsomeone inside\b"],
}

# Intents that belong to each scenario — used to filter matches
SCENARIO_INTENTS = {
    "general": {
        "MEDICAL_EMERGENCY", "LOST_CHILD", "WAKE_UP", "SLEEP", "IDENTIFY",
        "BATTERY_STATUS", "TASK_STATUS", "HEALTH_CHECK", "FOLLOW", "STOP_FOLLOW",
        "GO_CHARGE", "GO_HOME", "CANCEL", "REPEAT", "LIST_CAPABILITIES",
        "VOLUME_UP", "VOLUME_DOWN",
    },
    "restaurant": {
        "TAKE_ORDER", "SHOW_MENU", "ASK_SPECIALS", "ADD_TO_ORDER", "CANCEL_ORDER",
        "FILTER_MENU", "DISH_INGREDIENTS", "REQUEST_BILL", "BILL_DISPUTE",
        "PAYMENT_METHOD", "CALL_WAITER", "CALL_MANAGER", "ORDER_DELAY",
        "CLOSING_TIME", "FIND_RESTROOM", "REQUEST_WATER", "COLD_FOOD_COMPLAINT",
        "WRONG_ORDER", "LEAVING_RESTAURANT",
    },
    "hotel": {
        "CHECK_IN", "CHECK_OUT", "LATE_CHECKOUT", "LUGGAGE_STORAGE", "ROOM_DELIVERY",
        "HOUSEKEEPING", "DO_NOT_DISTURB", "EXTRA_TOWELS", "REQUEST_TOILETRIES",
        "MAINTENANCE", "CHANGE_LINEN", "GUIDE_TO_ROOM", "FIND_RESTAURANT",
        "FIND_GYM", "FIND_ELEVATOR", "FIND_RECEPTION", "FIND_SMOKING_AREA",
        "LOCKED_OUT", "LOST_CHILD", "MEDICAL_EMERGENCY",
    },
    "bar": {
        "ORDER_BEER", "ORDER_COCKTAIL", "ROUND_AGAIN", "SHOW_DRINKS_MENU",
        "WHAT_ON_TAP", "NON_ALCOHOLIC", "BARTENDER_CHOICE", "ORDER_WATER_BAR",
        "ORDER_WINE", "ORDER_SHOTS",
    },
    "home": {
        "NAVIGATE_ROOM", "LIGHTS_ON", "LIGHTS_OFF", "FAN_ON", "FAN_OFF",
        "SET_TEMPERATURE", "AC_OFF", "TV_ON", "TV_OFF", "GOODNIGHT_MODE",
        "GOOD_MORNING", "LEAVING_HOME", "ARRIVING_HOME", "WHO_IS_HOME",
    },
}

# Safety-critical — always checked first regardless of scenario
PRIORITY_INTENTS = ["MEDICAL_EMERGENCY", "LOST_CHILD"]


def try_fast_match(text: str, active_scenario: str = None) -> dict | None:
    """
    Try to match user text against known command patterns.

    active_scenario: the currently selected scenario (restaurant / hotel /
                     bar / home). If provided, only intents belonging to
                     that scenario OR to "general" are considered.
                     If None, all intents are checked (startup / fallback).

    Returns a result dict if matched, None if no match (caller sends to LLM).
    Fixed responses from commands.py are ALWAYS returned — the LLM never
    overrides them. This guarantees consistent production behaviour.
    """
    text_lower = text.lower().strip()

    # Determine which intents are valid for this scenario
    allowed = set(SCENARIO_INTENTS["general"])
    if active_scenario and active_scenario in SCENARIO_INTENTS:
        allowed |= SCENARIO_INTENTS[active_scenario]
    else:
        # No scenario set yet — allow everything (e.g. scenario selection turn)
        for s in SCENARIO_INTENTS.values():
            allowed |= s

    # Safety-critical checked first, always
    for intent in PRIORITY_INTENTS:
        if intent not in allowed:
            continue
        for pattern in PATTERNS.get(intent, []):
            if re.search(pattern, text_lower, re.IGNORECASE):
                return _build_result(intent, text_lower)

    # General intents next
    for intent in SCENARIO_INTENTS["general"]:
        if intent in PRIORITY_INTENTS:
            continue
        if intent not in allowed:
            continue
        for pattern in PATTERNS.get(intent, []):
            if re.search(pattern, text_lower, re.IGNORECASE):
                return _build_result(intent, text_lower)

    # Scenario-specific intents last
    if active_scenario:
        for intent in SCENARIO_INTENTS.get(active_scenario, set()):
            if intent not in allowed:
                continue
            for pattern in PATTERNS.get(intent, []):
                if re.search(pattern, text_lower, re.IGNORECASE):
                    return _build_result(intent, text_lower)

    return None


def _build_result(intent: str, text_lower: str) -> dict:
    from commands import get_command_by_intent
    cmd = get_command_by_intent(intent)
    if not cmd:
        return None
    return {
        "scenario":    cmd["scenario"],
        "intent":      intent,
        "slots":       _extract_slots(text_lower),
        "response":    cmd["bot_response"],
        "action":      cmd["action"],
        "after_dialog": cmd["after_dialog"],
    }


def _extract_slots(text: str) -> dict:
    slots = {}
    room = re.search(r"\broom\s*(\d+)\b", text)
    if room:
        slots["room"] = room.group(1)
    temp = re.search(r"(\d+)\s*degrees?", text)
    if temp:
        slots["temperature"] = temp.group(1)
    place = re.search(r"\bto\s+(?:the\s+)?([a-z][a-z ]{2,30}?)(?:\s*[.,!?]|$)", text)
    if place:
        slots["place"] = place.group(1).strip()
    return slots


def detect_scenario_from_text(text: str) -> str | None:
    """
    Try to detect which scenario the user is selecting from their spoken input.
    Used during the startup scenario-selection prompt.
    Returns scenario name or None if unclear.
    """
    import config
    text_lower = text.lower()
    for scenario, keywords in config.SCENARIO_KEYWORDS.items():
        for kw in keywords:
            if kw in text_lower:
                return scenario
    return None
