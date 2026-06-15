#!/usr/bin/env python3
"""
Argo Mini Voice Agent v2.1 (Production-Ready)

Improvements from v2:
? Hybrid Intent Classifier (fast path + LLM fallback)
? Conversation Context (memory across turns)
? Structured JSON Output (Ollama format=json)
? Latency Tracking (component-level metrics)
? Rich World Model (delivery tracking, task history)
? Correction Handling ("No, table 6" detection)
? State Awareness (where robot is, what it's doing)

Hardware: Jetson Orin NX, ROS 2 Humble
"""

import json
import logging
import os
import queue
import re
import subprocess
import threading
import time
from dataclasses import dataclass, field, asdict
from enum import Enum
from pathlib import Path
from typing import Optional, List, Dict, Tuple
from collections import deque

import numpy as np
import rclpy
from rclpy.node import Node
from std_msgs.msg import Int32, Float32, Bool, String
from geometry_msgs.msg import PoseWithCovarianceStamped

try:
    import sounddevice as sd
    import soundfile as sf
    import requests
except ImportError as exc:
    raise SystemExit(f"Missing deps: pip3 install sounddevice soundfile requests numpy") from exc

try:
    from vosk import Model as VoskModel, KaldiRecognizer
except ImportError as exc:
    raise SystemExit(
        "Vosk not installed: pip3 install vosk\n"
        "Download model: wget https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip"
    ) from exc


# ?????????????????????????????????????????????????????????????????????????????
# LOGGING & METRICS
# ?????????????????????????????????????????????????????????????????????????????

logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] [%(name)s] %(levelname)s: %(message)s'
)
logger = logging.getLogger("argo_voice_v2.1")


@dataclass
class Metrics:
    """Track component latencies."""
    wake_latency: float = 0.0
    stt_latency: float = 0.0
    intent_latency: float = 0.0
    tts_latency: float = 0.0
    total_latency: float = 0.0

    # Counters
    wake_detections: int = 0
    commands_processed: int = 0
    intent_llm_calls: int = 0  # Count how often we fall back to LLM
    intent_fast_path: int = 0  # Count fast path hits

    def log_summary(self):
        logger.info(
            f"Latencies: STT={self.stt_latency:.2f}s "
            f"Intent={self.intent_latency:.2f}s TTS={self.tts_latency:.2f}s "
            f"Total={self.total_latency:.2f}s | "
            f"Commands={self.commands_processed} "
            f"FastPath={self.intent_fast_path} "
            f"LLM={self.intent_llm_calls}"
        )


# ?????????????????????????????????????????????????????????????????????????????
# ROBOT STATE & WORLD MODEL
# ?????????????????????????????????????????????????????????????????????????????

class TaskStatus(Enum):
    IDLE = "idle"
    DELIVERING = "delivering"
    RETURNING_BASE = "returning_base"
    DOCKED = "docked"
    OBSTACLE = "obstacle"


@dataclass
class DeliveryTask:
    """Active delivery task."""
    destination_table: int
    start_time: float
    items: List[str] = field(default_factory=list)
    status: TaskStatus = TaskStatus.DELIVERING

    def elapsed_sec(self) -> float:
        return time.time() - self.start_time


@dataclass
class RobotState:
    """Rich world model of robot state."""
    battery_level: float = 100.0
    location_x: float = 0.0
    location_y: float = 0.0
    is_navigating: bool = False
    is_docked: bool = False

    # Delivery tracking
    current_task: Optional[DeliveryTask] = None
    last_table: Optional[int] = None
    task_history: deque = field(default_factory=lambda: deque(maxlen=10))

    # Navigation
    current_goal: Optional[int] = None

    def describe_state(self) -> str:
        """Generate natural language description of robot state."""
        if self.is_docked:
            return "I am docked and charging."

        if self.current_task:
            elapsed = self.current_task.elapsed_sec()
            if elapsed < 5:
                return f"I am heading to table {self.current_task.destination_table}."
            else:
                return f"I am at table {self.current_task.destination_table}."

        if self.is_navigating:
            if self.current_goal:
                return f"I am navigating to table {self.current_goal}."
            return "I am navigating."

        return "I am idle and ready to help."


@dataclass
class ConversationContext:
    """Track conversation state across turns."""
    last_table: Optional[int] = None
    last_intent: Optional[str] = None
    correction_mode: bool = False
    turn_count: int = 0
    conversation_history: deque = field(default_factory=lambda: deque(maxlen=5))
    last_speech: Optional[str] = None

    def add_turn(self, user_text: str, bot_response: str):
        """Record conversation turn."""
        self.conversation_history.append({
            "user": user_text,
            "bot": bot_response,
            "time": time.time(),
        })
        self.turn_count += 1
        self.last_speech = user_text

    def detect_correction(self, text: str) -> Tuple[bool, Optional[int]]:
        """Detect if user is correcting previous command.

        Examples:
        - "No, table 6" ? True, 6
        - "Actually, table 3" ? True, 3
        - "Change to table 5" ? True, 5
        """
        correction_prefixes = ["no", "actually", "wait", "change", "correction"]

        if not any(p in text.lower() for p in correction_prefixes):
            return False, None

        # Extract number
        match = re.search(r"table\s+(\d+)|(\d+)", text.lower())
        if match:
            table = int(match.group(1) or match.group(2))
            return True, table

        return False, None


# ?????????????????????????????????????????????????????????????????????????????
# HYBRID INTENT CLASSIFIER
# ?????????????????????????????????????????????????????????????????????????????

class HybridIntentClassifier:
    """Grammar-phrase lookup for fast intents + LLM only for general_chat responses.

    Vosk already constrains recognition to PHRASE_INTENTS keys, so classify_fast
    is a simple dict lookup ? no regex, no number normalization needed.
    LLM is only invoked when the recognised phrase is general_chat.
    """

    def __init__(self, ollama_host: str = "http://localhost:11434"):
        self.ollama_host = ollama_host
        self.session = requests.Session()
        self.metrics = Metrics()

    def classify_fast(self, text: str) -> Tuple[Optional[Dict], float]:
        """Exact lookup in PHRASE_INTENTS ? always O(1), always high confidence."""
        intent = PHRASE_INTENTS.get(text.lower().strip())
        if intent:
            return intent.copy(), 0.98
        return None, 0.0

    _SYSTEM_PROMPT = (
        'You are Argo Sonic, a restaurant delivery robot. '
        'Respond ONLY with JSON: {"actions":[{"intent":"..."}],"confidence":0.9}\n'
        'Intents: goto_table (add "table":<int>), return_base, call_waiter, ask_status, '
        'general_chat (add "response":"<Argo Sonic reply max 20 words>").\n'
        'Examples:\n'
        '"go to table 3"->{"actions":[{"intent":"goto_table","table":3}],"confidence":0.98}\n'
        '"how are you?"->{"actions":[{"intent":"general_chat","response":"I am Argo Sonic, fully charged and ready to serve!"}],"confidence":0.9}\n'
        '"tell me a joke"->{"actions":[{"intent":"general_chat","response":"Why do robots never get tired? Because they run on love and electricity!"}],"confidence":0.9}\n'
        '"what can you do?"->{"actions":[{"intent":"general_chat","response":"I am Argo Sonic! I navigate to tables, call waiters, and keep the restaurant running smoothly!"}],"confidence":0.9}\n'
        'User: '
    )

    def classify_llm(self, text: str) -> Tuple[Optional[Dict], float]:
        """LLM fallback ? classifies intent and generates response for general chat."""
        try:
            response = self.session.post(
                f"{self.ollama_host}/api/generate",
                json={
                    "model": "qwen2.5:1.5b",
                    "prompt": f"{self._SYSTEM_PROMPT}{text}",
                    "stream": False,
                    "temperature": 0.3,
                },
                timeout=8,
            )
            response.raise_for_status()

            result_text = response.json().get("response", "").strip()
            try:
                parsed = json.loads(result_text)
                actions = parsed.get("actions", [])
                confidence = float(parsed.get("confidence", 0.0))
                if actions:
                    return actions[0], confidence
            except (json.JSONDecodeError, ValueError):
                logger.warning(f"JSON parse failed: {result_text[:120]}")

        except Exception as e:
            logger.error(f"LLM classification failed: {e}")

        return None, 0.0

    def classify(self, text: str, prefer_fast: bool = True) -> Tuple[Optional[Dict], float, bool]:
        """Classify recognised text.

        - Navigation/service phrases ? instant lookup, no LLM
        - general_chat phrases ? fast lookup for intent type, LLM for response text
        - Unknown phrase ? LLM full classification (shouldn't happen with Vosk grammar)

        Returns: (intent, confidence, used_llm)
        """
        intent, conf = self.classify_fast(text)
        if intent:
            if intent.get("intent") == "general_chat":
                # We know it's chat; ask LLM to generate the spoken reply
                trigger = intent.get("trigger", text)
                llm_intent, llm_conf = self.classify_llm(trigger)
                self.metrics.intent_llm_calls += 1
                if llm_intent and llm_intent.get("intent") == "general_chat":
                    return llm_intent, llm_conf, True
                # LLM failed ? fall back to a generic Argo Sonic reply
                intent["response"] = (
                    "I am Argo Sonic, your restaurant robot! "
                    "I can navigate to tables, call a waiter, or bring the bill!"
                )
                return intent, conf, False
            self.metrics.intent_fast_path += 1
            return intent, conf, False

        # Grammar miss (shouldn't happen with Vosk) ? fall back to full LLM
        logger.info(f"[CLASS] Grammar miss, querying LLM: '{text[:50]}'")
        intent, conf = self.classify_llm(text)
        self.metrics.intent_llm_calls += 1
        return intent, conf, True


# ?????????????????????????????????????????????????????????????????????????????
# VOSK STT ? WAKE & COMMAND GRAMMARS
# ?????????????????????????????????????????????????????????????????????????????

# Wake grammar: only these phrases trigger wakeup
WAKE_GRAMMAR = json.dumps([
    "sonic", "hello sonic", "argo", "hey argo", "hey sonic",
    "so nic", "so nick", "son ic",
    "[unk]",
])

_TABLE_WORDS = [
    ("one", 1), ("two", 2), ("three", 3), ("four", 4),
    ("five", 5), ("six", 6), ("seven", 7), ("eight", 8),
    ("nine", 9), ("ten", 10), ("eleven", 11), ("twelve", 12),
]


def _build_grammar_and_intents():
    """Build CMD_GRAMMAR (JSON string) and PHRASE_INTENTS (dict) together.

    Vosk only recognizes phrases in the grammar, so every user-speakable
    phrase must be listed here. Tables 1-12 are expanded programmatically.
    """
    phrases = []
    intents = {}

    def add(phrase, intent):
        phrases.append(phrase)
        intents[phrase] = intent

    # ?? Table navigation (10 phrasings � 12 tables = 120 phrases) ??????????
    for word, num in _TABLE_WORDS:
        intent = {"intent": "goto_table", "table": num}
        for tmpl in [
            f"go to table {word}",
            f"table {word}",
            f"navigate to table {word}",
            f"deliver to table {word}",
            f"take me to table {word}",
            f"take order at table {word}",
            f"food ready for table {word}",
            f"order ready for table {word}",
            f"send to table {word}",
            f"head to table {word}",
        ]:
            add(tmpl, intent)

    # ?? Return to base ??????????????????????????????????????????????????????
    for p in [
        "return to base", "go back to base", "back to base",
        "come back to base", "return to kitchen", "go to kitchen",
        "go home", "come home", "dock", "go back", "return home",
    ]:
        add(p, {"intent": "return_base"})

    # ?? Call waiter / need help ?????????????????????????????????????????????
    for p in [
        "call the waiter", "call a waiter", "need a waiter",
        "call the manager", "need the manager", "get the waiter",
        "need assistance", "need help here", "send someone over",
        "someone come here", "help needed", "need help",
    ]:
        add(p, {"intent": "call_waiter"})

    # ?? Take order ?????????????????????????????????????????????????????????
    for p in [
        "take my order", "ready to order", "i am ready to order",
        "we are ready to order", "take our order", "can i order",
        "we want to order", "take the order", "we are ready",
    ]:
        add(p, {"intent": "take_order"})

    # ?? Bill / payment ?????????????????????????????????????????????????????
    for p in [
        "bring the bill", "get the bill", "the bill please", "bill please",
        "check please", "ready to pay", "we want to pay",
        "can we have the bill", "request the bill",
        "bring check", "we will pay now", "payment please", "bring me the check",
    ]:
        add(p, {"intent": "request_bill"})

    # ?? Water / drinks ??????????????????????????????????????????????????????
    for p in [
        "bring water", "need water", "more water please",
        "bring some water", "water please", "bring drinks",
        "more drinks please", "refill the drinks", "bring more water",
        "we need water", "can we get water",
    ]:
        add(p, {"intent": "bring_water"})

    # ?? Menu ???????????????????????????????????????????????????????????????
    for p in [
        "bring the menu", "need the menu", "can i see the menu",
        "bring me the menu", "menu please", "get me the menu",
    ]:
        add(p, {"intent": "bring_menu"})

    # ?? Clear table ????????????????????????????????????????????????????????
    for p in [
        "clear the table", "clean the table", "remove the dishes",
        "take the plates", "clear the dishes", "table needs cleaning",
        "remove the empty plates",
    ]:
        add(p, {"intent": "clear_table"})

    # ?? Table ready (for new guests) ????????????????????????????????????????
    for p in [
        "table is ready", "table is clean", "new guests arrived",
        "guests are seated", "table ready",
    ]:
        add(p, {"intent": "table_ready"})

    # ?? Status ?????????????????????????????????????????????????????????????
    for p in [
        "where are you", "what are you doing",
        "battery status", "how much battery", "what is your battery",
        "are you available", "are you busy",
    ]:
        add(p, {"intent": "ask_status"})

    # ?? Cancel / stop ???????????????????????????????????????????????????????
    for p in [
        "cancel", "stop", "wait", "hold on",
        "never mind", "cancel that", "stop moving", "pause",
    ]:
        add(p, {"intent": "cancel"})

    # ?? General chat (LLM generates the actual spoken response) ?????????????
    for p in [
        # jokes & fun
        "tell me a joke", "say a joke", "tell a joke", "say something funny",
        "can you dance", "can you sing", "sing a song",
        # identity
        "who are you", "what is your name", "are you a robot",
        "tell me about yourself", "what can you do",
        "what are your capabilities",
        # greetings
        "good morning", "good afternoon", "good evening",
        "hello", "hi there", "hey there",
        # wellbeing
        "how are you", "how are you doing",
        "are you enjoying your job", "do you like working here",
        "how long have you been working here",
        # compliments & thanks
        "thank you", "thanks", "thank you very much",
        "great job", "good job", "well done", "amazing",
        "excellent service", "you are awesome", "you are great",
        "i love this robot",
        # restaurant questions
        "what is the special today", "any recommendations",
        "what is on the menu", "do you have a recommendation",
    ]:
        add(p, {"intent": "general_chat", "trigger": p})

    phrases.append("[unk]")
    return json.dumps(phrases), intents


CMD_GRAMMAR, PHRASE_INTENTS = _build_grammar_and_intents()

WAKE_WORDS = frozenset(["sonic", "argo"])


class VoskSTT:
    """Grammar-constrained Vosk STT ? zero hallucinations, instant streaming recognition.

    Uses two KaldiRecognizer instances:
    - wake_rec: tiny grammar (just wake words)
    - cmd_rec:  full CMD_GRAMMAR (all restaurant phrases)

    Recognizers are rebuilt fresh after each TTS playback to flush echo.
    """

    def __init__(self, model_path: str, sample_rate: int = 16000):
        if not os.path.exists(model_path):
            raise RuntimeError(
                f"Vosk model not found at '{model_path}'.\n"
                "Download: wget https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip\n"
                "Extract to the path above."
            )
        logger.info(f"[STT] Loading Vosk model from {model_path}...")
        self._model = VoskModel(model_path)
        self._sr = sample_rate
        logger.info("[STT] Vosk model ready ? grammar-constrained recognition active")

    def make_wake_rec(self) -> KaldiRecognizer:
        rec = KaldiRecognizer(self._model, self._sr)
        rec.SetGrammar(WAKE_GRAMMAR)
        return rec

    def make_cmd_rec(self) -> KaldiRecognizer:
        rec = KaldiRecognizer(self._model, self._sr)
        rec.SetGrammar(CMD_GRAMMAR)
        return rec


# ?????????????????????????????????????????????????????????????????????????????
# AUDIO & TTS ? pre-cached for near-instant responses
# ?????????????????????????????????????????????????????????????????????????????

class AudioOut:
    """TTS with pre-generated cache: common phrases play in <50ms instead of ~2s."""

    TTS_SR = 22050

    # Pre-generated at startup ? covers every response the robot ever says
    COMMON_PHRASES = [
        "Yes, I am here",
        "Going back to sleep",
        "Argo version two point one is ready",
        "Returning to base",
        "Sorry, which table number?",
        "Could you repeat that?",
        "Calling a waiter. One moment please.",
        "Sorry, I didn't understand that.",
        # New service-intent responses
        "Order noted! A waiter will be right with you.",
        "I will get your bill. One moment please.",
        "Bringing water right away!",
        "I will bring the menu for you!",
        "I will have someone clear the table for you.",
        "Table is ready. Noted!",
        "Cancelled. Going back to sleep.",
    ] + [f"Heading to table {n}" for n in range(1, 13)]

    def __init__(self, piper_model: str = "en_US-lessac-medium"):
        self.piper_model = piper_model
        self.speaking = threading.Event()
        self.sounds = self._init_sounds()
        self._cache: Dict[str, np.ndarray] = {}
        # Pre-build cache in background ? startup is not delayed
        threading.Thread(target=self._prebuild_cache, daemon=True).start()

    # ?? Sound effects ??????????????????????????????????????????????????????

    def _init_sounds(self) -> dict:
        sr = 16000
        sounds = {}

        t = np.linspace(0, 0.1, int(sr * 0.1))
        beep = (np.sin(2 * np.pi * 1000 * t) * 0.3).astype(np.float32)
        sounds["wake"] = np.concatenate([beep, np.zeros(int(sr * 0.05)), beep])

        t = np.linspace(0, 0.2, int(sr * 0.2))
        ding = (np.sin(2 * np.pi * 800 * t) * np.exp(-t) * 0.3).astype(np.float32)
        sounds["ding"] = ding

        chime = []
        for freq, dur in [(523, 0.15), (659, 0.15), (784, 0.3)]:
            t = np.linspace(0, dur, int(sr * dur))
            chime.append((np.sin(2 * np.pi * freq * t) * 0.2).astype(np.float32))
            chime.append(np.zeros(int(sr * 0.05)))
        sounds["chime"] = np.concatenate(chime)

        return sounds

    def play_sound(self, name: str):
        if name in self.sounds:
            try:
                sd.play(self.sounds[name], samplerate=16000, blocking=False)
            except Exception as e:
                logger.warning(f"Sound play failed: {e}")

    # ?? Piper TTS ??????????????????????????????????????????????????????????

    def _piper_generate(self, text: str) -> Optional[np.ndarray]:
        """Run Piper and return int16 PCM array. Returns None on failure."""
        try:
            proc = subprocess.Popen(
                ["/home/argo/piper/piper", "--model", self.piper_model, "--output-raw"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            data, err = proc.communicate(input=text.encode("utf-8"), timeout=10)
            if err:
                logger.warning(f"Piper stderr: {err.decode(errors='replace')[:300]}")
            arr = np.frombuffer(data, dtype=np.int16).copy()
            if len(arr) == 0:
                logger.error(f"Piper returned empty audio for: '{text[:40]}'")
                return None
            return arr
        except Exception as e:
            logger.error(f"Piper failed for '{text[:40]}': {e}")
            return None

    def _prebuild_cache(self):
        logger.info(f"[TTS] Pre-building {len(self.COMMON_PHRASES)} cached responses...")
        for phrase in self.COMMON_PHRASES:
            audio = self._piper_generate(phrase)
            if audio is not None:
                self._cache[phrase.lower()] = audio
        logger.info("[TTS] Cache ready ? all responses are now instant")

    def say(self, text: str, blocking: bool = False) -> float:
        """Speak text. Cache hit ? ~50ms. Cache miss ? runs Piper (~0.5-1s + audio)."""
        start = time.time()
        try:
            self.speaking.set()
            audio = self._cache.get(text.lower())
            if audio is None:
                audio = self._piper_generate(text)
                if audio is not None:
                    self._cache[text.lower()] = audio
            if audio is not None:
                duration = len(audio) / self.TTS_SR
                sd.stop()  # prevent concurrent play from other threads (timer vs audio_thread)
                sd.play(audio, samplerate=self.TTS_SR, blocking=False)
                # Keep speaking=True for audio duration + 1.0s echo tail so room
                # reverb dies before the mic opens for command collection
                time.sleep(duration + 1.0)
        except Exception as e:
            logger.error(f"TTS say() failed: {e}")
        finally:
            self.speaking.clear()
        return time.time() - start

    def is_speaking(self) -> bool:
        return self.speaking.is_set()


# ?????????????????????????????????????????????????????????????????????????????
# MAIN VOICE AGENT v2.1
# ?????????????????????????????????????????????????????????????????????????????

class VoiceAgentV21(Node):
    """Argo voice agent v2.1 with hybrid classification, memory, and world model."""

    SLEEPING = "sleeping"
    LISTENING = "listening"
    PROCESSING = "processing"

    def __init__(self):
        super().__init__("voice_agent_v2_1")

        # Parameters
        self.declare_parameter("sample_rate", 16000)
        self.declare_parameter("wake_timeout", 30.0)
        self.declare_parameter("ollama_host", "http://localhost:11434")
        self.declare_parameter("piper_model", "/home/argo/piper-voices/en_US-lessac-medium.onnx")
        self.declare_parameter(
            "vosk_model_path",
            "/home/argo/dhruvil/argo_mini_ws/src/argo_mini/argo_mini/STT_project/vosk-model-small-en-us-0.15",
        )

        self._sr = self.get_parameter("sample_rate").value
        self._wake_timeout = self.get_parameter("wake_timeout").value
        self._state = self.SLEEPING
        self._state_lock = threading.Lock()

        # Initialize components
        logger.info("[INIT] Loading v2.1 components (Vosk STT)...")

        self.classifier = HybridIntentClassifier(
            self.get_parameter("ollama_host").value
        )
        self.audio_out = AudioOut(
            self.get_parameter("piper_model").value
        )
        self.stt = VoskSTT(
            self.get_parameter("vosk_model_path").value,
            self._sr,
        )

        # State management
        self.robot_state = RobotState()
        self.conversation = ConversationContext()
        self._state_lock_robot = threading.Lock()

        # ROS Publishers
        self._pub_waypoint = self.create_publisher(
            Int32, "/dashboard_waypoint_cmd", 10
        )
        self._pub_metrics = self.create_publisher(
            String, "/robot/voice_metrics", 10
        )

        # ROS Subscribers
        self.create_subscription(
            Float32, "/battery_level", self._on_battery, 10
        )
        self.create_subscription(
            PoseWithCovarianceStamped, "/amcl_pose", self._on_pose, 10
        )
        self.create_subscription(
            Int32, "/nav_status", self._on_nav_status, 10
        )

        # Audio loop
        self._audio_q = queue.Queue()
        self._timer = None

        threading.Thread(target=self._audio_thread, daemon=True).start()

        logger.info("[READY] Voice agent v2.1 online")
        self.audio_out.play_sound("wake")
        self.audio_out.say("Argo version two point one is ready")

    # ??? State Management ?????????????????????????????????????????????????????

    def _set_state(self, state: str):
        with self._state_lock:
            self._state = state

    def _get_state(self) -> str:
        with self._state_lock:
            return self._state

    def _reset_wake_timer(self):
        if self._timer:
            self._timer.cancel()
        self._timer = threading.Timer(
            self._wake_timeout,
            self._on_wake_timeout
        )
        self._timer.start()

    def _on_wake_timeout(self):
        with self._state_lock:
            if self._state != self.LISTENING:
                return  # already SLEEPING or PROCESSING ? don't fire spuriously
            self._state = self.SLEEPING
        self.audio_out.say("Going back to sleep")
        logger.info("[STATE] Sleeping (timeout)")

    # ??? ROS Callbacks ????????????????????????????????????????????????????????

    def _on_battery(self, msg: Float32):
        with self._state_lock_robot:
            self.robot_state.battery_level = msg.data
            if msg.data < 20:
                logger.warning(f"Low battery: {msg.data}%")

    def _on_pose(self, msg: PoseWithCovarianceStamped):
        with self._state_lock_robot:
            self.robot_state.location_x = msg.pose.pose.position.x
            self.robot_state.location_y = msg.pose.pose.position.y

    def _on_nav_status(self, msg: Int32):
        with self._state_lock_robot:
            self.robot_state.is_navigating = msg.data > 0

    def _audio_callback(self, indata, frames, time_info, status):
        if status:
            logger.warning(f"Audio status: {status}")
        self._audio_q.put(bytes(indata))

    # ??? Intent Execution ????????????????????????????????????????????????????

    def _execute_intent(self, intent: Dict, confidence: float) -> bool:
        """Execute parsed intent with conversation memory."""

        if confidence < 0.3:
            self.audio_out.say("Sorry, I didn't understand that.")
            return False

        intent_type = intent.get("intent")

        # Check for corrections
        is_correction, corrected_table = self.conversation.detect_correction(
            self.conversation.last_speech or ""
        )

        if is_correction and intent_type == "goto_table" and corrected_table:
            logger.info(f"Correction detected: {self.robot_state.last_table} ? {corrected_table}")
            intent["table"] = corrected_table

        # Execute based on intent type
        if intent_type == "goto_table":
            raw_table = intent.get("table")
            try:
                table = int(raw_table)
            except (TypeError, ValueError):
                logger.warning(f"[INTENT] Bad table value from LLM: {raw_table!r}, ignoring")
                self.audio_out.say("Sorry, which table number?")
                return False
            self._publish_waypoint(table)
            self.audio_out.play_sound("ding")

            # Update state
            with self._state_lock_robot:
                self.robot_state.current_task = DeliveryTask(
                    destination_table=table,
                    start_time=time.time(),
                )
                self.robot_state.last_table = table

            self.audio_out.say(f"Heading to table {table}")
            self.conversation.last_intent = intent_type
            self.conversation.last_table = table
            return True

        elif intent_type == "return_base":
            self._publish_waypoint(0)
            self.audio_out.play_sound("ding")

            with self._state_lock_robot:
                self.robot_state.current_task = DeliveryTask(
                    destination_table=0,
                    start_time=time.time(),
                )

            self.audio_out.say("Returning to base")
            return True

        elif intent_type == "call_waiter":
            self.audio_out.say(
                "Calling a waiter. One moment please."
            )
            # TODO: Integrate with POS
            return True

        elif intent_type == "ask_status":
            with self._state_lock_robot:
                status = self.robot_state.describe_state()
                battery = self.robot_state.battery_level

            response = f"{status} Battery is at {battery:.0f} percent."
            self.audio_out.say(response)
            return True

        elif intent_type == "take_order":
            self.audio_out.say("Order noted! A waiter will be right with you.")
            return True

        elif intent_type == "request_bill":
            self.audio_out.say("I will get your bill. One moment please.")
            return True

        elif intent_type == "bring_water":
            self.audio_out.say("Bringing water right away!")
            return True

        elif intent_type == "bring_menu":
            self.audio_out.say("I will bring the menu for you!")
            return True

        elif intent_type == "clear_table":
            self.audio_out.say("I will have someone clear the table for you.")
            return True

        elif intent_type == "table_ready":
            self.audio_out.say("Table is ready. Noted!")
            return True

        elif intent_type == "cancel":
            self.audio_out.say("Cancelled. Going back to sleep.")
            return True

        elif intent_type == "general_chat":
            reply = intent.get("response", "").strip()
            if not reply:
                reply = "I am Argo Sonic, your friendly delivery robot. How can I help?"
            logger.info(f"[CHAT] '{reply}'")
            self.audio_out.say(reply)
            return True

        return False

    def _publish_waypoint(self, waypoint: int):
        msg = Int32()
        msg.data = int(waypoint)
        self._pub_waypoint.publish(msg)
        logger.info(f"[PUB] Waypoint {waypoint}")

    def _publish_metrics(self):
        """Publish component latencies for monitoring."""
        metrics_dict = asdict(self.classifier.metrics)
        msg = String()
        msg.data = json.dumps(metrics_dict)
        self._pub_metrics.publish(msg)

    # ??? Audio Processing Loop (Vosk streaming) ??????????????????????????????

    def _audio_thread(self):
        """Main audio processing thread using Vosk streaming recognition.

        Vosk handles VAD and utterance segmentation internally, so we no
        longer need a sliding window, silence accumulator, or RMS thresholds.
        Recognizers are rebuilt fresh after each TTS playback to flush echo.
        """
        logger.info("[AUDIO] Thread started (Vosk streaming mode)")

        wake_rec = self.stt.make_wake_rec()
        cmd_rec  = self.stt.make_cmd_rec()
        was_speaking = False

        def drain_queue():
            while not self._audio_q.empty():
                try:
                    self._audio_q.get_nowait()
                except Exception:
                    break

        with sd.RawInputStream(
            samplerate=self._sr, blocksize=4000,   # 250ms chunks
            dtype="int16", channels=1,
            callback=self._audio_callback,
        ):
            while rclpy.ok():
                try:
                    data = self._audio_q.get(timeout=0.5)
                    currently_speaking = self.audio_out.is_speaking()

                    if currently_speaking:
                        was_speaking = True
                        continue  # ignore mic input while TTS is playing

                    if was_speaking and not currently_speaking:
                        # TTS just finished ? drain residual echo and reset recognizers
                        was_speaking = False
                        drain_queue()
                        wake_rec = self.stt.make_wake_rec()
                        cmd_rec  = self.stt.make_cmd_rec()
                        continue

                    state = self._get_state()

                    # ?? SLEEPING: stream to wake recognizer ???????????????????
                    if state == self.SLEEPING:
                        if wake_rec.AcceptWaveform(data):
                            text = json.loads(wake_rec.Result()).get("text", "").lower()
                            if text and text != "[unk]":
                                logger.info(f"[WAKE_SCAN] '{text}'")
                            if any(w in text for w in WAKE_WORDS):
                                logger.info(f"[WAKE] Detected: '{text}'")
                                self.classifier.metrics.wake_detections += 1
                                self._set_state(self.LISTENING)
                                wake_rec = self.stt.make_wake_rec()
                                cmd_rec  = self.stt.make_cmd_rec()
                                self.audio_out.play_sound("wake")
                                self.audio_out.say("Yes, I am here")
                                drain_queue()
                                cmd_rec = self.stt.make_cmd_rec()
                                self._reset_wake_timer()

                    # ?? LISTENING: stream to command recognizer ???????????????
                    elif state == self.LISTENING:
                        if cmd_rec.AcceptWaveform(data):
                            text = json.loads(cmd_rec.Result()).get("text", "").lower().strip()
                            logger.info(f"[CMD] Vosk: '{text}'")
                            if text and text != "[unk]":
                                self._handle_recognized(text)
                                cmd_rec = self.stt.make_cmd_rec()
                        else:
                            partial = json.loads(cmd_rec.PartialResult()).get("partial", "")
                            if partial:
                                logger.debug(f"[CMD_PARTIAL] '{partial}'")

                except queue.Empty:
                    pass
                except Exception as e:
                    logger.error(f"[AUDIO_THREAD] {e}", exc_info=True)

    def _handle_recognized(self, text: str):
        """Handle Vosk-recognised phrase: classify intent and execute it."""
        e2e_start = time.time()
        logger.info(f"[STT] '{text}'")

        self._set_state(self.PROCESSING)
        self.conversation.last_speech = text

        intent, conf, _ = self.classifier.classify(text)

        # drain audio that queued during LLM call (general_chat only, ~1-5s)
        while not self._audio_q.empty():
            try:
                self._audio_q.get_nowait()
            except Exception:
                break

        if intent:
            self._execute_intent(intent, conf)
            self.conversation.add_turn(text, "Acknowledged")
            if self._timer:
                self._timer.cancel()
            self._set_state(self.SLEEPING)
        else:
            self.audio_out.say("Could you repeat that?")
            while not self._audio_q.empty():
                try:
                    self._audio_q.get_nowait()
                except Exception:
                    break
            self._reset_wake_timer()
            self._set_state(self.LISTENING)

        self.classifier.metrics.total_latency = time.time() - e2e_start
        self.classifier.metrics.commands_processed += 1
        self.classifier.metrics.log_summary()
        self._publish_metrics()


def main(args=None):
    rclpy.init(args=args)
    node = VoiceAgentV21()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
