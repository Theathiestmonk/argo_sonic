"""Fixed phrases. They are pre-synthesised at start-up (and disk-cached), so they play with ~0 latency."""
import random

GREETINGS = ["Hi there!", "Wassup!", "How can I help you?", "Hello! What can I do for you?", "Hey! How can I help?"]
NOT_HEARD = ["Sorry, I didn't catch that. Could you say it again?", "Sorry, could you repeat that?"]
SLEEP = ["Okay, call me if you need anything."]
ASK_LOCATION = ["Where do you want me to go?"]
ASK_TABLE = ["Which table is this for?"]
ASK_ITEM = ["What would you like to order?"]
ASK_MORE = ["Anything else?"]
ASK_CATEGORY = ["Which category would you like to hear about?"]
BLOCKED = ["Sorry, I am blocked by something in that direction and cannot move."]
NO_SENSORS = ["Sorry, my safety sensors are not responding, so I can't move right now."]
NOT_AVAILABLE_NAV = ["Sorry, I could not get there."]
NO_PROBLEM = ["No problem."]
ERROR = ["Sorry, something went wrong. Please try again."]
ORDER_SENT_FAIL = ["Sorry, I couldn't send the order. Please call a staff member."]
MOVING = {"forward": "Moving forward.", "backward": "Moving back.", "left": "Turning left and moving.",
          "right": "Turning right and moving.", "spin": "Spinning!", "turn_left": "Turning left.", "turn_right": "Turning right."}

ALL_FIXED = (GREETINGS + NOT_HEARD + SLEEP + ASK_LOCATION + ASK_TABLE + ASK_ITEM + ASK_MORE
             + ASK_CATEGORY + BLOCKED + NO_SENSORS + NOT_AVAILABLE_NAV + NO_PROBLEM + ERROR + ORDER_SENT_FAIL
             + list(MOVING.values()))


def pick(options: list[str]) -> str:
    return random.choice(options)
