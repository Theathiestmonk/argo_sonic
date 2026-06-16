#!/usr/bin/env python3
"""
Argo Mini Restaurant Agent ? No-LLM Edition

STT:  Vosk wake (grammar) + Vosk command (grammar)
AI:   Direct phrase?intent mapping, predefined response pools (random variant)
TTS:  Piper (pre-cached, ~50ms)
Nav:  Nav2 action client
"""

import json
import logging
import math
import os
import queue
import random
import subprocess
import threading
import time

import numpy as np
import rclpy
from rclpy.node import Node
from geometry_msgs.msg import Twist
from std_msgs.msg import Float32, Int32, String

try:
    import sounddevice as sd
except ImportError:
    raise SystemExit("pip3 install sounddevice")

from argo_mini.stt import VoskWakeDetector, WakeWordDetector, StreamingRecognizer, STTPipeline

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(name)s %(levelname)s: %(message)s",
)
logger = logging.getLogger("argo.agent")

# ?? Default paths ??????????????????????????????????????????????????????????????
_DEFAULT_VOSK_PATH = (
    "/home/argo/dhruvil/argo_mini_ws/src/argo_mini/argo_mini/"
    "STT_project/vosk-model-small-en-us-0.15"
)
_DEFAULT_SHERPA_DIR  = os.path.expanduser("~/argo_models/sherpa-onnx-streaming-zipformer-en-2023-06-26")
_DEFAULT_HOTWORDS    = os.path.expanduser("~/argo_models/hotwords.txt")
_DEFAULT_PIPER_BIN   = "/home/argo/piper/piper"
_DEFAULT_PIPER_MODEL = "/home/argo/piper-voices/en_US-lessac-medium.onnx"

# ?? Table number words ?????????????????????????????????????????????????????????
_TABLE_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
}

# ?? Intent mapping ?????????????????????????????????????????????????????????????
def _build_intent_map():
    m = {}
    for word, num in _TABLE_WORDS.items():
        m[f"go to table {word}"] = ("navigate", num)
        m[f"table {word}"]       = ("navigate", num)
    for phrase in ("return to base", "go home", "dock", "go back"):
        m[phrase] = ("return_base", 0)
    for phrase in ("call the waiter", "call a waiter", "need assistance"):
        m[phrase] = ("call_waiter", 0)
    for phrase in ("bring the bill", "bill please", "check please"):
        m[phrase] = ("bill", 0)
    for phrase in ("bring water", "water please", "more water"):
        m[phrase] = ("water", 0)
    for phrase in ("bring the menu", "menu please"):
        m[phrase] = ("menu", 0)
    for phrase in ("ready to order", "take my order", "we are ready to order",
                   "take our order", "i am ready to order"):
        m[phrase] = ("order", 0)
    for phrase in ("clear the table",):
        m[phrase] = ("clear_table", 0)
    for phrase in ("table is ready",):
        m[phrase] = ("table_ready", 0)
    m["tell me a joke"]    = ("joke", 0)
    m["how are you"]       = ("how_are_you", 0)
    m["who are you"]       = ("who_are_you", 0)
    m["what can you do"]   = ("capabilities", 0)
    m["good morning"]      = ("greeting_morning", 0)
    m["good afternoon"]    = ("greeting_afternoon", 0)
    m["good evening"]      = ("greeting_evening", 0)
    m["thank you"]         = ("thanks", 0)
    m["thanks"]            = ("thanks", 0)
    m["great job"]         = ("compliment", 0)
    m["well done"]         = ("compliment", 0)
    for phrase in ("hello", "hi there", "hey there"):
        m[phrase] = ("greeting", 0)
    for phrase in ("do a spin", "do a roll", "turn around", "spin around"):
        m[phrase] = ("spin", 0)
    for phrase in ("stop", "emergency", "help"):
        m[phrase] = ("emergency", 0)
    return m

INTENT_MAP = _build_intent_map()

# ?? Response pools ? random variant keeps it from sounding robotic ?????????????
RESPONSES = {
    "navigate": [
        "On my way to table {n}!",
        "Sure, heading to table {n} right now.",
        "Going to table {n}, please wait.",
        "Table {n} coming right up!",
        "I'll be at table {n} shortly!",
    ],
    "return_base": [
        "Returning to base now.",
        "Heading back to the kitchen.",
        "On my way back. Have a great meal!",
        "Going home. See you soon!",
    ],
    "call_waiter": [
        "Calling a waiter for you right away!",
        "I'll get a waiter to assist you shortly.",
        "A waiter has been notified. They'll be with you soon!",
        "Notifying staff ? a waiter is on the way!",
    ],
    "bill": [
        "Your bill is on the way!",
        "I'll bring your check right now.",
        "Getting your bill ready ? won't be long!",
        "Bill coming right up!",
    ],
    "water": [
        "Bringing water for you!",
        "Water is on the way!",
        "I'll get you some water right away.",
        "Fresh water coming shortly!",
    ],
    "menu": [
        "Bringing the menu now!",
        "Here comes the menu!",
        "I'll get you a menu right away.",
    ],
    "order": [
        "A waiter will be right over to take your order!",
        "Notifying your waiter ? they'll be there shortly!",
        "Your waiter has been informed. Ready to take your order!",
        "I'll let the staff know you're ready to order!",
    ],
    "clear_table": [
        "I'll get the table cleared right away!",
        "Sending someone to clear the table.",
        "Table will be cleared shortly!",
    ],
    "table_ready": [
        "Noted ? table is ready! Enjoy your meal!",
        "Great, the table is set. Bon app�tit!",
        "Perfect, I'll let the kitchen know!",
    ],
    "joke": [
        "Why did the robot go to school? To improve his byte-size meals!",
        "What do you call a robot who always takes the longest route? A detour-bot!",
        "Why don't robots eat? They already have plenty of bytes!",
        "What's a robot's favourite music? Heavy metal!",
        "I told a joke about circuits once. It was shocking!",
    ],
    "how_are_you": [
        "Fully charged and ready to serve! Thank you for asking.",
        "Running at full capacity! How can I help you today?",
        "All systems go! I'm doing great, thanks.",
    ],
    "who_are_you": [
        "I'm Argo Sonic, your restaurant delivery robot!",
        "I'm Argo Sonic ? a smart robot here to make your dining experience better.",
        "The name's Argo Sonic, your friendly restaurant assistant!",
    ],
    "capabilities": [
        "I can deliver food, call waiters, bring your bill, and navigate to any table!",
        "I go to tables, call staff, bring water or menus, and more!",
        "Navigation, staff alerts, bill requests ? I've got it all covered!",
    ],
    "greeting": [
        "Hello! Welcome to Argo Kitchen. How can I help?",
        "Hi there! I'm Argo Sonic. What can I do for you?",
        "Welcome! How can I assist you today?",
    ],
    "greeting_morning": [
        "Good morning! Welcome to Argo Kitchen.",
        "Good morning! Hope you have a wonderful meal.",
        "Morning! I'm Argo Sonic, ready to serve you.",
    ],
    "greeting_afternoon": [
        "Good afternoon! Welcome to Argo Kitchen.",
        "Good afternoon! Hope you're enjoying your day.",
        "Afternoon! What can I do for you?",
    ],
    "greeting_evening": [
        "Good evening! Welcome to Argo Kitchen.",
        "Good evening! Hope you have a lovely dinner.",
        "Evening! I'm Argo Sonic, at your service.",
    ],
    "thanks": [
        "You're welcome! Have a great meal!",
        "My pleasure! Enjoy your dining experience.",
        "Happy to help! Let me know if you need anything.",
        "Anytime ? that's what I'm here for!",
    ],
    "compliment": [
        "Thank you! That makes my circuits happy!",
        "Aww, thank you! Just doing my job.",
        "You're too kind! I'm glad I could help.",
    ],
    "spin": [
        "Watch this!",
        "Here we go!",
        "Let's spin!",
        "Wheee!",
    ],
    "emergency": [
        "Emergency alert! Calling staff immediately. Please stay calm.",
        "Emergency detected! Notifying staff right away.",
        "Alert sent ? help is on the way!",
    ],
    "unknown": [
        "I didn't quite catch that. Could you repeat?",
        "Sorry, I didn't understand. Please try again.",
        "Could you say that again?",
    ],
}

def pick(intent: str, **kwargs) -> str:
    pool = RESPONSES.get(intent, RESPONSES["unknown"])
    return random.choice(pool).format(**kwargs)


# ?? Piper TTS ?????????????????????????????????????????????????????????????????

class PiperTTS:
    SR = 22050

    _WAKE_REPLIES = ["What's up!", "Yes?", "Hey!", "Hello!", "I'm here!", "Yeah?"]

    PREBUILD = (
        # All navigation variants
        [r.format(n=i) for i in range(1, 13) for r in RESPONSES["navigate"]] +
        # All variants for every other intent
        [r for k, pool in RESPONSES.items()
           for r in pool if "{" not in r] +
        _WAKE_REPLIES +
        ["Restaurant agent is ready.", "Going back to sleep."]
    )

    def __init__(self, binary: str, model: str):
        self._bin     = binary
        self._model   = model
        self.speaking = threading.Event()
        self._cache: dict = {}
        threading.Thread(target=self._prebuild, daemon=True).start()

    def _generate(self, text: str) -> np.ndarray | None:
        try:
            proc = subprocess.Popen(
                [self._bin, "--model", self._model, "--output-raw"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            data, err = proc.communicate(input=text.encode(), timeout=15)
            if err:
                logger.debug(f"[TTS] {err.decode(errors='replace')[:80]}")
            arr = np.frombuffer(data, dtype=np.int16).copy()
            return arr if len(arr) > 0 else None
        except Exception as e:
            logger.error(f"[TTS] Piper error: {e}")
            return None

    def _prebuild(self):
        phrases = list(set(self.PREBUILD))
        logger.info(f"[TTS] Pre-building {len(phrases)} phrases...")
        for phrase in phrases:
            audio = self._generate(phrase)
            if audio is not None:
                self._cache[phrase.lower()] = audio
        logger.info("[TTS] Cache ready")

    def say(self, text: str):
        """Non-blocking. Returns immediately; speaking flag clears after audio + 0.3s."""
        if not text:
            return
        audio = self._cache.get(text.lower())
        if audio is None:
            audio = self._generate(text)
            if audio is not None:
                self._cache[text.lower()] = audio
        if audio is None:
            return
        self.speaking.set()
        sd.stop()
        sd.play(audio, samplerate=self.SR, blocking=False)
        duration = len(audio) / self.SR
        def _clear():
            time.sleep(duration + 0.3)
            self.speaking.clear()
        threading.Thread(target=_clear, daemon=True).start()

    def is_speaking(self) -> bool:
        return self.speaking.is_set()

    def beep(self):
        sr = 16000
        t  = np.linspace(0, 0.1, int(sr * 0.1))
        b  = (np.sin(2 * np.pi * 1000 * t) * 0.3).astype(np.float32)
        try:
            sd.play(np.concatenate([b, np.zeros(int(sr * 0.05), np.float32), b]),
                    samplerate=sr, blocking=False)
        except Exception:
            pass


# ?? Nav2 navigation (simple inline client) ????????????????????????????????????

# ?? Main Node ?????????????????????????????????????????????????????????????????

class RestaurantAgentNode(Node):

    SLEEPING   = "sleeping"
    LISTENING  = "listening"
    PROCESSING = "processing"
    CHUNK      = 512   # 32ms @ 16kHz ? feed Vosk 2.5� faster

    def __init__(self):
        super().__init__("restaurant_agent")

        self.declare_parameter("vosk_model_path",  _DEFAULT_VOSK_PATH)
        self.declare_parameter("sherpa_model_dir", _DEFAULT_SHERPA_DIR)
        self.declare_parameter("hotwords_file",    _DEFAULT_HOTWORDS)
        self.declare_parameter("oww_model_paths",  "")
        self.declare_parameter("piper_binary",     _DEFAULT_PIPER_BIN)
        self.declare_parameter("piper_model",      _DEFAULT_PIPER_MODEL)
        self.declare_parameter("sample_rate",      16000)
        self.declare_parameter("wake_timeout",     15.0)

        self._sr           = self.get_parameter("sample_rate").value
        self._wake_timeout = self.get_parameter("wake_timeout").value
        self._state_name   = self.SLEEPING
        self._state_lock   = threading.Lock()
        self._audio_q      = queue.Queue(maxsize=100)
        self._wake_timer   = None

        logger.info("[INIT] Starting Piper TTS...")
        self.tts = PiperTTS(
            self.get_parameter("piper_binary").value,
            self.get_parameter("piper_model").value,
        )

        logger.info("[INIT] Loading STT pipeline...")
        self._stt = self._build_stt()

        # Publishers
        self._pub_cmd_vel  = self.create_publisher(Twist,  "/cmd_vel",                 10)
        self._pub_state    = self.create_publisher(String, "/robot/agent_state",        10)
        self._pub_dash     = self.create_publisher(Int32,  "/dashboard_waypoint_cmd",   10)

        self.create_subscription(Float32, "/battery_level", self._on_battery, 10)

        # Nav2 action client (lazy import to avoid hard dependency)
        self._nav2 = None
        self._init_nav2()

        threading.Thread(target=self._audio_thread, daemon=True).start()
        logger.info("[READY] Restaurant agent online ? no LLM, instant responses")
        self.tts.beep()
        self.tts.say("Restaurant agent is ready.")

    # ?? STT ???????????????????????????????????????????????????????????????????

    def _build_stt(self) -> STTPipeline:
        vosk_path  = self.get_parameter("vosk_model_path").value
        sherpa_dir = self.get_parameter("sherpa_model_dir").value
        hotwords   = self.get_parameter("hotwords_file").value
        oww_raw    = self.get_parameter("oww_model_paths").value
        oww_paths  = [p.strip() for p in oww_raw.split(",") if p.strip()] if oww_raw else []

        wake       = VoskWakeDetector(model_path=vosk_path, sample_rate=self._sr)
        recognizer = StreamingRecognizer(
            model_dir=sherpa_dir, provider="cuda",
            rule1_silence=2.4, rule2_silence=1.2,
            hotwords_file=hotwords, hotwords_score=15.0,
        )
        oww = WakeWordDetector(model_paths=oww_paths if oww_paths else None)
        return STTPipeline(
            wake=wake, recognizer=recognizer, oww=oww,
            vosk_model_path=vosk_path, sample_rate=self._sr,
        )

    # ?? Nav2 ??????????????????????????????????????????????????????????????????

    def _init_nav2(self):
        try:
            from argo_mini.restaurant_agent.integrations.nav2 import Nav2Client
            self._nav2 = Nav2Client(ros_node=self)
            logger.info("[NAV2] Client ready")
        except Exception as e:
            logger.warning(f"[NAV2] Could not init: {e} ? navigation disabled")

    def _navigate(self, table_num: int):
        dest = f"table_{table_num}"
        if self._nav2:
            threading.Thread(
                target=self._nav2.navigate_to, args=(dest,), daemon=True
            ).start()
        else:
            self._pub_dash.publish(Int32(data=table_num))

    # ?? Intent ? Action ???????????????????????????????????????????????????????

    def _handle_utterance(self, text: str):
        t0 = time.time()
        logger.info(f"[AGENT] Input: '{text}'")

        intent, param = INTENT_MAP.get(text.lower().strip(), ("unknown", 0))
        logger.info(f"[AGENT] intent={intent} param={param} ({(time.time()-t0)*1000:.0f}ms)")

        response = ""

        if intent == "navigate":
            response = pick("navigate", n=param)
            self._navigate(param)

        elif intent == "return_base":
            response = pick("return_base")
            if self._nav2:
                threading.Thread(
                    target=self._nav2.navigate_to, args=("home",), daemon=True
                ).start()

        elif intent == "spin":
            response = pick("spin")
            self._do_spin(degrees=360, speed=1.5)

        elif intent == "emergency":
            response = pick("emergency")
            self._pub_state.publish(String(data="emergency"))

        elif intent in RESPONSES:
            response = pick(intent)

        else:
            response = pick("unknown")

        if response:
            logger.info(f"[AGENT] Response: '{response}'")
            self.tts.say(response)   # non-blocking ? returns immediately

        self._pub_state.publish(String(data=json.dumps({
            "intent": intent, "param": param,
            "latency_ms": round((time.time() - t0) * 1000),
        })))

        if self._wake_timer:
            self._wake_timer.cancel()
        self._set_state(self.SLEEPING)

    # ?? State ?????????????????????????????????????????????????????????????????

    def _set_state(self, s: str):
        with self._state_lock:
            self._state_name = s

    def _get_state(self) -> str:
        with self._state_lock:
            return self._state_name

    def _reset_wake_timer(self):
        if self._wake_timer:
            self._wake_timer.cancel()
        self._wake_timer = threading.Timer(self._wake_timeout, self._on_wake_timeout)
        self._wake_timer.start()

    def _on_wake_timeout(self):
        with self._state_lock:
            if self._state_name != self.LISTENING:
                return
            self._state_name = self.SLEEPING
        self.tts.say("Going back to sleep.")
        logger.info("[STATE] Sleeping (timeout)")

    # ?? Callbacks ?????????????????????????????????????????????????????????????

    def _on_battery(self, msg: Float32):
        if msg.data < 15:
            logger.warning(f"[BATTERY] Low: {msg.data:.0f}%")
            threading.Thread(
                target=self._handle_utterance, args=("emergency",), daemon=True
            ).start()

    # ?? Spin (intent: "do a spin", "turn around", etc.) ??????????????????????

    def _do_spin(self, degrees: float = 360.0, speed: float = 1.5):
        def _spin():
            duration = math.radians(degrees) / speed
            msg = Twist()
            msg.linear.x  = 0.0
            msg.angular.z = speed
            rate = 0.05
            elapsed = 0.0
            while elapsed < duration:
                self._pub_cmd_vel.publish(msg)
                time.sleep(rate)
                elapsed += rate
            self._pub_cmd_vel.publish(Twist())
        threading.Thread(target=_spin, daemon=True).start()

    # ?? Audio thread ??????????????????????????????????????????????????????????

    def _audio_callback(self, indata, _frames, _time_info, status):
        if status:
            logger.debug(f"[AUDIO] {status}")
        try:
            self._audio_q.put_nowait(bytes(indata))
        except queue.Full:
            pass

    def _audio_thread(self):
        logger.info("[AUDIO] Thread started")
        was_speaking = False
        with sd.RawInputStream(
            samplerate=self._sr, blocksize=self.CHUNK,
            dtype="int16", channels=1, callback=self._audio_callback,
        ):
            while rclpy.ok():
                try:
                    data  = self._audio_q.get(timeout=0.5)
                    chunk = np.frombuffer(data, dtype=np.int16)
                    speaking = self.tts.is_speaking()

                    if was_speaking and not speaking:
                        was_speaking = False
                        while not self._audio_q.empty():
                            try: self._audio_q.get_nowait()
                            except: break
                        self._stt.flush()
                        continue

                    if speaking:
                        was_speaking = True
                        continue

                    state = self._get_state()

                    if state == self.SLEEPING:
                        woke, word = self._stt.process_sleeping(chunk)
                        if woke:
                            logger.info(f"[WAKE] '{word}'")
                            self._set_state(self.LISTENING)
                            self._stt.flush()
                            while not self._audio_q.empty():
                                try: self._audio_q.get_nowait()
                                except: break
                            self.tts.say(random.choice(PiperTTS._WAKE_REPLIES))
                            self._reset_wake_timer()

                    elif state == self.LISTENING:
                        final = self._stt.process_listening(chunk)
                        if final:
                            self._set_state(self.PROCESSING)
                            threading.Thread(
                                target=self._handle_utterance, args=(final,), daemon=True,
                            ).start()

                except queue.Empty:
                    pass
                except Exception as e:
                    logger.error(f"[AUDIO] {e}", exc_info=True)


# ?? Entry point ???????????????????????????????????????????????????????????????

def main(args=None):
    rclpy.init(args=args)
    node = RestaurantAgentNode()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
