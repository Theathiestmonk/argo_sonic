"""Zero-LLM shortcuts for utterances that are unambiguous. Anything doubtful returns None
and falls through to the LLM classifier. Saves ~300-600 ms on the commonest commands."""
from __future__ import annotations

import re
from typing import Optional

_P = r"(?:please\s+)?"
_MOVE = re.compile(
    rf"^{_P}(?:move|go|step|walk|come|turn|take a step|go a bit|move a bit)?\s*(?:one step |a little |a bit |a step )?"
    r"(?:to the |towards the )?(forward|forwards|ahead|straight|back|backward|backwards|reverse|left|right)(?: side)?(?: please)?$")
_SPIN = re.compile(rf"^{_P}(?:do a |do one |make a |take a )?(?:spin|spin around|rotate|twirl|do a spin|turn around)(?: please)?$")
_YES = re.compile(r"^(?:yes|yeah|yep|yup|sure|ok|okay|ya|haan|han|ha|correct|right|please|of course|absolutely|go ahead|confirm|yes please|ok sure)\W*$")
_NO = re.compile(r"^(?:no|nope|nah|nahi|not now|nothing|done|i'?m good|that'?s (?:all|it)|that is (?:all|it)|that will be all|nothing else)"
                 r"(?:[ ,]+(?:thanks|thank you|please|that'?s all|that'?s it|that is all|nothing else|for now|i'?m good|sir|madam))*\W*$")
_BYE = re.compile(r"^(?:bye|goodbye|good bye|see you|that'?s all for now|go to sleep|stop listening)\W*$")

# "go to table 1 and take order" / "come to table 4 then deliver the order" (zero-LLM, English)
_TASK_TAKE = r"(?:take|taking|collect|get|note|record)(?: down)?(?: the| an| a| their| his| her| my| our)?(?: new)? ?orders?"
_TASK_DELIVER = (r"(?:deliver|delivering|serve|bring|give|drop)(?: the| an| a| their| his| her| my| our)?"
                 r"(?: food| order| orders| dishes| items| parcel| tray)?(?: order| food)?(?: over)?")
_COMPOUND = re.compile(
    rf"^{_P}(?:go|come|move|drive|head|navigate|walk|run|take me)(?: over)?(?: straight)? (?:to|at|towards) (?:the )?(?P<loc>.+?)"
    rf"(?:,)? (?:and|then|and then|&)(?: then)? (?:{_P})(?:(?P<take>{_TASK_TAKE})|(?P<dlv>{_TASK_DELIVER}))(?: there| here| please)?$")
_TASK_ONLY = re.compile(rf"^{_P}(?:(?P<take>{_TASK_TAKE})|(?P<dlv>(?:deliver|serve)(?: the| this| an| a)?(?: food| order| orders)?))(?: please)?$")

_DIR = {"forward": "forward", "forwards": "forward", "ahead": "forward", "straight": "forward",
        "back": "backward", "backward": "backward", "backwards": "backward", "reverse": "backward",
        "left": "left", "right": "right"}


def blank_ent() -> dict:
    return {"intent": "unclear", "reply": "none", "location": None, "table_number": None, "items": [],
            "movement_direction": None, "menu_category": None, "menu_item": None, "menu_query": None,
            "diet": None, "wants_specials": False, "wants_famous": False, "general_topic": None,
            "remove_items": [], "cancel_order": False, "task": None}


def try_fast(text: str, awaiting: Optional[str]) -> Optional[dict]:
    t = re.sub(r"\s+", " ", text.lower().strip().rstrip(".!?"))
    if not t:
        return None
    if _BYE.match(t):
        return {**blank_ent(), "intent": "end"}
    if awaiting:
        if _YES.match(t):
            return {**blank_ent(), "reply": "yes"}
        if _NO.match(t):
            return {**blank_ent(), "reply": "no"}
        return None
    m = _COMPOUND.match(t)
    if m:
        return {**blank_ent(), "intent": "navigation", "location": m.group("loc").strip(),
                "task": "take_order" if m.group("take") else "deliver"}
    m = _TASK_ONLY.match(t)
    if m:
        task = "take_order" if m.group("take") else "deliver"
        return {**blank_ent(), "intent": "ordering" if task == "take_order" else "delivery", "task": task}
    m = _MOVE.match(t)
    if m:
        w = m.group(1)
        if len(t.split()) == 1 and w in ("left", "right"):
            return None          # bare "right" is as likely "correct" – let the LLM decide
        # "turn left/right" = rotate in place; bare "left/right/go left" = move that way
        if t.split()[0] == "turn" and w in ("left", "right"):
            return {**blank_ent(), "intent": "movement", "movement_direction": f"turn_{w}"}
        return {**blank_ent(), "intent": "movement", "movement_direction": _DIR[w]}
    if _SPIN.match(t):
        return {**blank_ent(), "intent": "movement", "movement_direction": "spin"}
    return None


_blank = blank_ent
