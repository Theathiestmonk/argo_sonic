"""
commands.py
-----------
Full command/intent registry for Sonic robot across all 5 scenarios.
Each entry has:
  - intent       : internal intent name
  - scenario     : which domain this belongs to
  - examples     : sample utterances used in the system prompt for the LLM
  - bot_response : what Sonic says (may include {slot} placeholders)
  - action       : what the robot physically does
  - after_dialog : follow-up line after action completes
"""

SCENARIOS = ["general", "restaurant", "hotel", "bar", "home"]

COMMANDS = [
    # ─────────────────────────── GENERAL ───────────────────────────
    {
        "intent": "WAKE_UP",
        "scenario": "general",
        "examples": ["hey sonic", "wake up sonic", "hey argo", "hi sonic"],
        "bot_response": "Hello! How can I help?",
        "action": "enable_listening",
        "after_dialog": None,
    },
    {
        "intent": "SLEEP",
        "scenario": "general",
        "examples": ["go to sleep", "take a rest", "standby mode"],
        "bot_response": "Going to standby. I'll wake up when you call.",
        "action": "disable_listening",
        "after_dialog": None,
    },
    {
        "intent": "IDENTIFY",
        "scenario": "general",
        "examples": ["what's your name", "who are you", "are you a robot", "what are you"],
        "bot_response": "Hi! I'm Sonic, your robot assistant. I'm here to help!",
        "action": "speak_identity",
        "after_dialog": "What else can I do?",
    },
    {
        "intent": "BATTERY_STATUS",
        "scenario": "general",
        "examples": ["how much battery do you have", "battery status", "are you charged", "are you going to die"],
        "bot_response": "I have {battery_hours} hours of battery remaining.",
        "action": "get_battery_level",
        "after_dialog": None,
    },
    {
        "intent": "TASK_STATUS",
        "scenario": "general",
        "examples": ["what are you doing", "are you busy", "what's your task", "where are you going"],
        "bot_response": "I am currently {current_task}. I'll be free in a moment.",
        "action": "get_current_task",
        "after_dialog": None,
    },
    {
        "intent": "HEALTH_CHECK",
        "scenario": "general",
        "examples": ["are you okay", "is something wrong", "are you working", "you seem broken"],
        "bot_response": "All systems are running fine, thank you!",
        "action": "speak_health_status",
        "after_dialog": None,
    },
    {
        "intent": "GO_CHARGE",
        "scenario": "general",
        "examples": ["go charge", "charge yourself", "go to your dock"],
        "bot_response": "Heading to my charging dock.",
        "action": "go_to_dock",
        "after_dialog": "Fully charged and ready to help you again.",
    },
    {
        "intent": "GO_HOME",
        "scenario": "general",
        "examples": ["go to home", "return to base", "go to your base"],
        "bot_response": "Heading back to my home position.",
        "action": "go_to_base",
        "after_dialog": "I am home.",
    },
    {
        "intent": "FOLLOW",
        "scenario": "general",
        "examples": ["follow me", "come with me", "walk with me"],
        "bot_response": "Following you now.",
        "action": "follow_person",
        "after_dialog": None,
    },
    {
        "intent": "STOP_FOLLOW",
        "scenario": "general",
        "examples": ["stop following", "don't follow", "stay here", "wait here"],
        "bot_response": "I'm waiting. Call me when you need me.",
        "action": "stop_following",
        "after_dialog": None,
    },
    {
        "intent": "NAVIGATE_TO",
        "scenario": "general",
        "examples": ["come here", "come to the lobby", "go to table 3", "over here", "go to {place}"],
        "bot_response": "On my way to {place}.",
        "action": "navigate_to",
        "after_dialog": "I've arrived.",
    },
    {
        "intent": "REPEAT",
        "scenario": "general",
        "examples": ["repeat that", "say that again", "i didn't hear you", "what did you say"],
        "bot_response": "I said: {last_utterance}",
        "action": "repeat_last_tts",
        "after_dialog": None,
    },
    {
        "intent": "CANCEL",
        "scenario": "general",
        "examples": ["cancel", "never mind", "forget it", "ignore that", "abort"],
        "bot_response": "Got it, cancelled.",
        "action": "cancel_current_task",
        "after_dialog": None,
    },
    {
        "intent": "LIST_CAPABILITIES",
        "scenario": "general",
        "examples": ["what can you do", "how can you help me", "show me your features"],
        "bot_response": "I can navigate, deliver items, call staff, and more. Want a full list?",
        "action": "speak_capabilities",
        "after_dialog": None,
    },
    {
        "intent": "VOLUME_UP",
        "scenario": "general",
        "examples": ["speak louder", "i can't hear you", "turn up your volume", "louder please"],
        "bot_response": "Increasing volume.",
        "action": "increase_volume",
        "after_dialog": None,
    },
    {
        "intent": "VOLUME_DOWN",
        "scenario": "general",
        "examples": ["speak softer", "too loud", "lower your voice", "quiet down"],
        "bot_response": "Decreasing volume.",
        "action": "decrease_volume",
        "after_dialog": None,
    },

    # ─────────────────────────── RESTAURANT ───────────────────────────
    {
        "intent": "TAKE_ORDER",
        "scenario": "restaurant",
        "examples": ["we are ready to order", "can we order now", "take our order", "i'd like to order"],
        "bot_response": "Great! I'll note your order. Tell me what you'd like.",
        # No action/after_dialog here on purpose — this used to fire take_order()
        # (a full kitchen hand-off sequence) immediately on this trigger phrase,
        # before any item was even known, then speak "Your order is on the way."
        # The actual hand-off now happens once the order is finalized (main.py's
        # order-taking branch, triggered by REQUEST_BILL or a closing cue) —
        # this line's only job is to open the ordering flow.
        "action": "none",
        "after_dialog": None,
    },
    {
        "intent": "SHOW_MENU",
        "scenario": "restaurant",
        "examples": ["can i see the menu", "show me the menu", "what's on the menu", "bring the menu"],
        "bot_response": "Wait a second, I'll bring you a menu.",
        "action": "bring_menu",
        "after_dialog": "Here it is. Want me to tell you about today's specials?",
    },
    {
        "intent": "ASK_SPECIALS",
        "scenario": "restaurant",
        "examples": ["what are today's specials", "any specials today", "chef's special", "what do you recommend"],
        "bot_response": "Today's specials are: {specials}.",
        "action": "speak_specials",
        "after_dialog": "Would you like to order one of these?",
    },
    {
        "intent": "ADD_TO_ORDER",
        "scenario": "restaurant",
        "examples": ["i want to add something", "can i add to my order", "one more item please"],
        "bot_response": "Of course! What would you like to add?",
        "action": "add_to_order",
        "after_dialog": "Got it! I'll update your order.",
    },
    {
        "intent": "CANCEL_ORDER",
        "scenario": "restaurant",
        "examples": ["cancel my order", "i don't want that anymore", "remove that item"],
        "bot_response": "I'll notify the waiter for cancellation.",
        "action": "cancel_order",
        "after_dialog": "Your order cancellation is being processed.",
    },
    {
        "intent": "FILTER_MENU",
        "scenario": "restaurant",
        "examples": ["do you have vegetarian options", "any vegan dishes", "show me gluten free options"],
        "bot_response": "Let me filter the menu for you.",
        "action": "filter_menu",
        "after_dialog": "Here are all the options that match. Want to know more about any dish?",
    },
    {
        "intent": "DISH_INGREDIENTS",
        "scenario": "restaurant",
        "examples": ["what's in this dish", "does it have nuts", "ingredients please", "is it spicy"],
        "bot_response": "Let me check that for you.",
        "action": "query_dish_ingredients",
        "after_dialog": "Anything else you'd like to know before ordering?",
    },
    {
        "intent": "REQUEST_BILL",
        "scenario": "restaurant",
        "examples": ["bill please", "get me the check", "we want to pay", "bring the bill"],
        "bot_response": "I'll get your bill ready right now.",
        "action": "bring_bill",
        "after_dialog": "Will you be paying by card or cash?",
    },
    {
        "intent": "BILL_DISPUTE",
        "scenario": "restaurant",
        "examples": ["i think the bill is wrong", "this doesn't look right", "extra charge on bill"],
        "bot_response": "I'm sorry about that! Let me get the manager for you right away.",
        "action": "call_manager",
        "after_dialog": "The manager is on their way. Don't worry, we'll sort this out.",
    },
    {
        "intent": "PAYMENT_METHOD",
        "scenario": "restaurant",
        "examples": ["do you take card", "can i pay in cash", "what payment methods", "do you accept upi"],
        "bot_response": "We accept UPI, all major cards, and cash.",
        "action": "speak_payment_methods",
        "after_dialog": "Which would you prefer?",
    },
    {
        "intent": "CALL_WAITER",
        "scenario": "restaurant",
        "examples": ["call the waiter", "send someone over", "i need a waiter", "excuse me"],
        "bot_response": "Calling your waiter now!",
        "action": "notify_waiter",
        "after_dialog": "Your waiter has been notified. They'll be with you shortly!",
    },
    {
        "intent": "CALL_MANAGER",
        "scenario": "restaurant",
        "examples": ["get me the manager", "i want to speak to the manager", "manager please"],
        "bot_response": "I'll get the manager for you right away.",
        "action": "call_manager",
        "after_dialog": "The manager has been notified and is on their way.",
    },
    {
        "intent": "ORDER_DELAY",
        "scenario": "restaurant",
        "examples": ["where is our order", "our order hasn't come", "it's been a long time", "we've been waiting"],
        "bot_response": "I'm sorry for the wait! I'll send an urgent reminder for your order.",
        "action": "notify_chef_urgency",
        "after_dialog": "Your order will arrive soon.",
    },
    {
        "intent": "CLOSING_TIME",
        "scenario": "restaurant",
        "examples": ["what time do you close", "what are your hours", "are you open late"],
        "bot_response": "We're open until 11 PM tonight. Kitchen closes at 10:30 PM.",
        "action": "speak_hours",
        "after_dialog": None,
    },
    {
        "intent": "FIND_RESTROOM",
        "scenario": "restaurant",
        "examples": ["where is the toilet", "where's the bathroom", "restroom please", "washroom"],
        "bot_response": "The restrooms are to your left.",
        "action": "speak_directions",
        "after_dialog": None,
    },
    {
        "intent": "REQUEST_WATER",
        "scenario": "restaurant",
        "examples": ["bring water", "we need water", "more water please", "refill water"],
        "bot_response": "Bringing water to your table right away!",
        "action": "deliver_water",
        "after_dialog": "Here's your water!",
    },
    {
        "intent": "COLD_FOOD_COMPLAINT",
        "scenario": "restaurant",
        "examples": ["my food is cold", "this is not hot enough", "cold food", "this dish is cold"],
        "bot_response": "I'm very sorry about that! I'll get your waiter right away.",
        "action": "notify_waiter",
        "after_dialog": "Your waiter is coming now. We'll make this right.",
    },
    {
        "intent": "WRONG_ORDER",
        "scenario": "restaurant",
        "examples": ["this doesn't look right", "wrong dish", "that's not mine", "different order came"],
        "bot_response": "I'm sorry about that! Let me get the manager for you right away.",
        "action": "notify_manager",
        "after_dialog": "We'll sort this out for you immediately.",
    },
    {
        "intent": "LEAVING_RESTAURANT",
        "scenario": "restaurant",
        "examples": ["we're leaving now", "we're done", "ready to go", "thank you goodbye"],
        "bot_response": "Thank you so much for dining with us! Hope to see you again soon.",
        "action": "speak_goodbye",
        "after_dialog": "I'd love to hear your feedback!",
    },

    # ─────────────────────────── HOTEL ───────────────────────────
    {
        "intent": "CHECK_IN",
        "scenario": "hotel",
        "examples": ["i want to check in", "we just arrived", "check in please"],
        "bot_response": "Welcome! Let me pull up your reservation. What name is it under?",
        "action": "check_reservation",
        "after_dialog": "You're all checked in! Your room is {room}. Let me show you the way.",
    },
    {
        "intent": "CHECK_OUT",
        "scenario": "hotel",
        "examples": ["i want to check out", "we're leaving today", "check out please"],
        "bot_response": "I'll prepare your checkout summary right away.",
        "action": "notify_manager_checkout",
        "after_dialog": "Here's your bill. Any extras to add?",
    },
    {
        "intent": "LATE_CHECKOUT",
        "scenario": "hotel",
        "examples": ["can i do late checkout", "extend my stay", "stay one more hour"],
        "bot_response": "Let me check availability for you.",
        "action": "check_late_checkout",
        "after_dialog": "Late checkout until 2 PM is available. Shall I confirm?",
    },
    {
        "intent": "LUGGAGE_STORAGE",
        "scenario": "hotel",
        "examples": ["can i store my luggage", "keep my bags please", "luggage storage"],
        "bot_response": "Of course! I'll arrange luggage storage for you.",
        "action": "take_luggage",
        "after_dialog": "Your luggage is stored safely. Tag number: LG-47.",
    },
    {
        "intent": "ROOM_DELIVERY",
        "scenario": "hotel",
        "examples": ["bring dinner to my room", "bring breakfast to room 204", "deliver food to my room"],
        "bot_response": "I'm on my way to room {room} with your {item}.",
        "action": "room_delivery",
        "after_dialog": "Here you go! Anything else you need?",
    },
    {
        "intent": "HOUSEKEEPING",
        "scenario": "hotel",
        "examples": ["clean my room", "housekeeping please", "make up my room"],
        "bot_response": "I'll schedule housekeeping for your room right away.",
        "action": "notify_housekeeping",
        "after_dialog": "Housekeeping will be with you shortly. Should take about 20 minutes.",
    },
    {
        "intent": "DO_NOT_DISTURB",
        "scenario": "hotel",
        "examples": ["do not disturb", "don't send anyone up", "privacy please"],
        "bot_response": "Understood. I've set Do Not Disturb for your room.",
        "action": "set_dnd",
        "after_dialog": "From now on, no one will disturb you.",
    },
    {
        "intent": "EXTRA_TOWELS",
        "scenario": "hotel",
        "examples": ["send extra towels", "more towels please", "i need a bath towel"],
        "bot_response": "I'll have fresh towels sent up right away.",
        "action": "deliver_towels",
        "after_dialog": "Here are your fresh towels!",
    },
    {
        "intent": "REQUEST_TOILETRIES",
        "scenario": "hotel",
        "examples": ["i need toiletries", "more shampoo please", "bring soap", "need conditioner"],
        "bot_response": "I'll have toiletries delivered right away.",
        "action": "deliver_toiletries",
        "after_dialog": "Here are the things you wanted.",
    },
    {
        "intent": "MAINTENANCE",
        "scenario": "hotel",
        "examples": ["something in my room is broken", "light is not working", "the AC is broken", "fix the TV"],
        "bot_response": "I'm sorry about that! I'll send maintenance right away.",
        "action": "notify_maintenance",
        "after_dialog": "Maintenance is on their way. Expected in about 15 minutes.",
    },
    {
        "intent": "CHANGE_LINEN",
        "scenario": "hotel",
        "examples": ["change my bed sheets", "fresh sheets please", "linen change"],
        "bot_response": "I'll have fresh linen brought to your room.",
        "action": "deliver_linen",
        "after_dialog": "Here's your fresh linen!",
    },
    {
        "intent": "GUIDE_TO_ROOM",
        "scenario": "hotel",
        "examples": ["take me to my room", "show me to room 204", "guide me to my room"],
        "bot_response": "Follow me! I'll take you right there.",
        "action": "navigate_to_room",
        "after_dialog": "Here's your room! Have a wonderful stay.",
    },
    {
        "intent": "FIND_RESTAURANT",
        "scenario": "hotel",
        "examples": ["where is the restaurant", "take me to the dining hall", "find the dining room"],
        "bot_response": "The restaurant is on the ground floor. Follow me!",
        "action": "navigate_to_restaurant",
        "after_dialog": "Here we are! The restaurant is open until 11 PM.",
    },
    {
        "intent": "FIND_GYM",
        "scenario": "hotel",
        "examples": ["where is the gym", "take me to the fitness centre", "find the gym"],
        "bot_response": "Follow me! I'll take you right there.",
        "action": "navigate_to_gym",
        "after_dialog": "Here's the gym. It's open from 6 AM to 10 PM. Enjoy your workout!",
    },
    {
        "intent": "FIND_ELEVATOR",
        "scenario": "hotel",
        "examples": ["where is the elevator", "find the lift", "which floor is the elevator"],
        "bot_response": "The elevator is just around the corner. I'll show you.",
        "action": "navigate_to_elevator",
        "after_dialog": "Elevator is here. Which floor would you like to go to?",
    },
    {
        "intent": "FIND_RECEPTION",
        "scenario": "hotel",
        "examples": ["take me to reception", "front desk please", "where is check-in counter"],
        "bot_response": "Sure! The reception is on the ground floor. Follow me.",
        "action": "navigate_to_reception",
        "after_dialog": "Here's the front desk. The team will assist you.",
    },
    {
        "intent": "FIND_SMOKING_AREA",
        "scenario": "hotel",
        "examples": ["where can i smoke", "smoking area please", "is there a smoking zone"],
        "bot_response": "Our smoking area is in the outdoor garden on the east side.",
        "action": "navigate_to_smoking_area",
        "after_dialog": "This is the designated smoking area. Please don't smoke elsewhere.",
    },
    {
        "intent": "MEDICAL_EMERGENCY",
        "scenario": "hotel",
        "examples": ["i need a doctor", "medical emergency", "someone is hurt", "call ambulance"],
        "bot_response": "Calling for medical help immediately! Please stay calm.",
        "action": "alert_emergency",
        "after_dialog": "Help is on the way. I'm coming to your room now. Stay where you are.",
    },
    {
        "intent": "LOCKED_OUT",
        "scenario": "hotel",
        "examples": ["i'm locked out of my room", "can't get in", "key not working", "lost my key"],
        "bot_response": "Don't worry! I'll get someone from the front desk right away.",
        "action": "notify_staff",
        "after_dialog": "A staff member is on the way with a key. Should be about 3 minutes.",
    },
    {
        "intent": "LOST_CHILD",
        "scenario": "hotel",
        "examples": ["child is missing", "i can't find my kid", "lost child", "my son is missing"],
        "bot_response": "We're on it immediately! Can you describe your child?",
        "action": "alert_security",
        "after_dialog": "All staff and security have been alerted. Please stay at the reception.",
    },

    # ─────────────────────────── BAR ───────────────────────────
    {
        "intent": "ORDER_BEER",
        "scenario": "bar",
        "examples": ["get me a beer", "one beer please", "i'll have a lager", "bring me a pint"],
        "bot_response": "Coming right up! Any particular brand or draught?",
        "action": "notify_bartender",
        "after_dialog": "Here is your beer!",
    },
    {
        "intent": "ORDER_COCKTAIL",
        "scenario": "bar",
        "examples": ["i want a cocktail", "make me a mojito", "gin and tonic please", "can i get a margarita"],
        "bot_response": "Great choice! I'll get that order in for you.",
        "action": "notify_bartender",
        "after_dialog": "Here is your order!",
    },
    {
        "intent": "ROUND_AGAIN",
        "scenario": "bar",
        "examples": ["another round please", "same again", "one more of the same", "repeat our drinks"],
        "bot_response": "Same drinks coming right up!",
        "action": "repeat_order",
        "after_dialog": "Here we go — your repeat order!",
    },
    {
        "intent": "SHOW_DRINKS_MENU",
        "scenario": "bar",
        "examples": ["can i see the drinks menu", "show me what you have", "drinks list please"],
        "bot_response": "Here's our full drinks menu!",
        "action": "bring_drinks_menu",
        "after_dialog": "Take your time and let me know what you'd like.",
    },
    {
        "intent": "WHAT_ON_TAP",
        "scenario": "bar",
        "examples": ["what's on tap", "draft beers available", "any craft beers", "draught beer list"],
        "bot_response": "We have 6 beers on tap tonight — here's the list.",
        "action": "speak_tap_list",
        "after_dialog": None,
    },
    {
        "intent": "NON_ALCOHOLIC",
        "scenario": "bar",
        "examples": ["i want something non-alcoholic", "no alcohol please", "virgin cocktail", "mocktail"],
        "bot_response": "Of course! Here are our non-alcoholic options.",
        "action": "bring_nonalcoholic_menu",
        "after_dialog": "Here is your order!",
    },
    {
        "intent": "BARTENDER_CHOICE",
        "scenario": "bar",
        "examples": ["surprise me", "bartender's choice", "whatever you recommend", "pick something for me"],
        "bot_response": "Love it! Any spirit preference or anything you don't like?",
        "action": "notify_bartender",
        "after_dialog": "Here is today's special!",
    },
    {
        "intent": "ORDER_WATER_BAR",
        "scenario": "bar",
        "examples": ["can i get water", "bring some water", "sparkling or still", "a glass of water"],
        "bot_response": "Still or sparkling?",
        "action": "deliver_water",
        "after_dialog": "Here's a bottle full of water!",
    },
    {
        "intent": "ORDER_WINE",
        "scenario": "bar",
        "examples": ["wine please", "red wine", "white wine", "bottle of rosé", "can i see the wine list"],
        "bot_response": "Of course! Here's our wine list.",
        "action": "bring_wine_menu",
        "after_dialog": "Here is the wine for you!",
    },
    {
        "intent": "ORDER_SHOTS",
        "scenario": "bar",
        "examples": ["shots please", "let's do shots", "tequila shots", "sambuca shots", "round of shots"],
        "bot_response": "How many shots and for how many people?",
        "action": "notify_bartender",
        "after_dialog": "Here are the shots for you!",
    },

    # ─────────────────────────── HOME / SMART HOME ───────────────────────────
    {
        "intent": "NAVIGATE_ROOM",
        "scenario": "home",
        "examples": ["take me to the kitchen", "take me to the living room", "go to the bedroom", "lead me to the garage"],
        "bot_response": "On my way. Follow me!",
        "action": "navigate_to_room",
        "after_dialog": "Here we are. Is there anything else?",
    },
    {
        "intent": "LIGHTS_ON",
        "scenario": "home",
        "examples": ["turn on the lights", "switch on lights", "lights on", "brighten the room"],
        "bot_response": "Turning on the lights.",
        "action": "send_command_lights_on",
        "after_dialog": "Lights are on. Anything else?",
    },
    {
        "intent": "LIGHTS_OFF",
        "scenario": "home",
        "examples": ["turn off the lights", "lights off", "switch off lights", "dim the lights"],
        "bot_response": "Turning off the lights.",
        "action": "send_command_lights_off",
        "after_dialog": "Lights are off. Anything else?",
    },
    {
        "intent": "FAN_ON",
        "scenario": "home",
        "examples": ["turn on the fan", "switch on fan", "start the fan", "fan on"],
        "bot_response": "Turning on the fan.",
        "action": "send_command_fan_on",
        "after_dialog": "Fan is on. Anything else?",
    },
    {
        "intent": "FAN_OFF",
        "scenario": "home",
        "examples": ["turn off the fan", "fan off", "stop the fan", "switch off fan"],
        "bot_response": "Turning off the fan.",
        "action": "send_command_fan_off",
        "after_dialog": "Fan is off. Anything else?",
    },
    {
        "intent": "SET_TEMPERATURE",
        "scenario": "home",
        "examples": ["set the temperature to 22", "make it warmer", "cool it down", "set ac to 18 degrees"],
        "bot_response": "Setting temperature to {temperature} degrees.",
        "action": "send_command_ac_temp",
        "after_dialog": "Done! Temperature is set. Anything else?",
    },
    {
        "intent": "AC_OFF",
        "scenario": "home",
        "examples": ["turn off the ac", "switch off ac", "stop cooling", "ac off"],
        "bot_response": "Turning off the air conditioner.",
        "action": "send_command_ac_off",
        "after_dialog": "AC is off. What else can I help with?",
    },
    {
        "intent": "TV_ON",
        "scenario": "home",
        "examples": ["turn on the tv", "switch on tv", "start tv", "tv on"],
        "bot_response": "Turning on the TV!",
        "action": "send_command_tv_on",
        "after_dialog": "TV is on. What would you like to watch?",
    },
    {
        "intent": "TV_OFF",
        "scenario": "home",
        "examples": ["turn off the tv", "tv off", "switch off tv", "tv sleep"],
        "bot_response": "Turning off the TV.",
        "action": "send_command_tv_off",
        "after_dialog": "TV is off. Anything else?",
    },
    {
        "intent": "GOODNIGHT_MODE",
        "scenario": "home",
        "examples": ["goodnight", "i'm going to sleep", "bedtime mode", "sleep mode", "night night"],
        "bot_response": "Goodnight! Setting up sleep mode.",
        "action": "activate_sleep_mode",
        "after_dialog": "Sleep tight! Everything is set.",
    },
    {
        "intent": "GOOD_MORNING",
        "scenario": "home",
        "examples": ["good morning", "wake up mode", "i'm awake", "morning sonic"],
        "bot_response": "Good morning! Let me get things ready.",
        "action": "activate_morning_mode",
        "after_dialog": "Here's your morning summary!",
    },
    {
        "intent": "LEAVING_HOME",
        "scenario": "home",
        "examples": ["i'm leaving", "goodbye", "lock up i'm going out", "i'm heading out", "bye"],
        "bot_response": "Have a great time! Securing the house.",
        "action": "activate_away_mode",
        "after_dialog": "House is secured. See you when you're back!",
    },
    {
        "intent": "ARRIVING_HOME",
        "scenario": "home",
        "examples": ["i'm home", "i'm back", "i've arrived", "open up i'm home"],
        "bot_response": "Welcome home! Setting things up.",
        "action": "activate_home_mode",
        "after_dialog": "Everything's ready for you. What do you need?",
    },
    {
        "intent": "WHO_IS_HOME",
        "scenario": "home",
        "examples": ["is anyone home", "who's home", "any motion detected", "is someone inside"],
        "bot_response": "Let me check the sensors.",
        "action": "query_presence_sensors",
        "after_dialog": "Sensor check complete.",
    },
]


def get_all_intents():
    return [c["intent"] for c in COMMANDS]


def get_intents_by_scenario(scenario: str):
    return [c for c in COMMANDS if c["scenario"] == scenario]


def get_command_by_intent(intent: str):
    for c in COMMANDS:
        if c["intent"] == intent:
            return c
    return None


def build_intent_examples_block():
    """Build a compact reference block for the LLM system prompt."""
    lines = []
    for scenario in SCENARIOS:
        lines.append(f"\n### Scenario: {scenario.upper()}")
        for c in COMMANDS:
            if c["scenario"] == scenario:
                examples_str = " | ".join(c["examples"][:3])
                lines.append(f"  Intent: {c['intent']}")
                lines.append(f"  Examples: {examples_str}")
    return "\n".join(lines)
