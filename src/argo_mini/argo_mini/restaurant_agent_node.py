#!/usr/bin/env python3
"""
Argo Mini Restaurant Agent Node ? Production STT Edition

STT pipeline (Alexa/OK Google quality, fully local):
  OpenWakeWord  ? always-on neural wake detection (<1% CPU, ONNX)
  Sherpa-ONNX   ? streaming full-vocabulary ASR (GPU, ~100ms latency)

Other components (unchanged):
  LangGraph     ? intent / ordering / navigation / billing / chat / emergency
  Piper TTS     ? pre-cached, near-instant speech output
  Nav2          ? ROS2 navigation action client

Run:
    # First time: download models
    bash scripts/download_models.sh

    # Launch
    ros2 run argo_mini restaurant_agent

Parameters (--ros-args -p key:=value):
    sherpa_model_dir : path to Sherpa-ONNX model directory
    hotwords_file    : path to hotwords.txt
    oww_model_paths  : comma-separated paths to custom .onnx wake models
    piper_binary     : path to piper binary
    piper_model      : path to .onnx voice model
    sample_rate      : audio sample rate (default 16000)
    wake_timeout     : seconds before going back to sleep (default 30)
    table_id         : current table (default "")
"""

import json
import logging
import os
import queue
import subprocess
import threading
import time

import math

import numpy as np
import rclpy
from rclpy.node import Node
from geometry_msgs.msg import Twist
from std_msgs.msg import Float32, String

try:
    import sounddevice as sd
except ImportError:
    raise SystemExit("pip3 install sounddevice")

from argo_mini.restaurant_agent.graph import build_graph
from argo_mini.restaurant_agent.state import AgentState, default_state
from argo_mini.stt import VoskWakeDetector, WakeWordDetector, StreamingRecognizer, STTPipeline

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(name)s %(levelname)s: %(message)s",
)
logger = logging.getLogger("argo.agent")

# ?? Default model paths (override via ROS params or env vars) ?????????????????
_DEFAULT_SHERPA_DIR  = os.environ.get(
    "SHERPA_MODEL_DIR",
    os.path.expanduser("~/argo_models/sherpa-onnx-streaming-zipformer-en-2023-06-26"),
)
_DEFAULT_HOTWORDS    = os.environ.get(
    "HOTWORDS_FILE",
    os.path.expanduser("~/argo_models/hotwords.txt"),
)
_DEFAULT_OWW_PATHS   = os.environ.get("OWW_MODEL_PATHS", "")  # comma-separated
_DEFAULT_VOSK_PATH   = (
    "/home/argo/dhruvil/argo_mini_ws/src/argo_mini/argo_mini/"
    "STT_project/vosk-model-small-en-us-0.15"
)
_DEFAULT_PIPER_BIN   = "/home/argo/piper/piper"
_DEFAULT_PIPER_MODEL = "/home/argo/piper-voices/en_US-lessac-medium.onnx"


# ?? Piper TTS ?????????????????????????????????????????????????????????????????

class PiperTTS:
    """Pre-cached Piper TTS ? cache hit ~50ms, cache miss ~0.8s."""

    SR = 22050

    COMMON = [
        "Yes, I am here",
        "Going back to sleep",
        "Restaurant agent is ready",
        "Returning to base",
        "Could you repeat that?",
        "I didn't understand that.",
        "Welcome to Argo Kitchen!",
        "How can I help you today?",
    ] + [f"Heading to table {n}" for n in range(1, 13)]

    def __init__(self, binary: str, model: str):
        self._bin = binary
        self._model = model
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
        logger.info(f"[TTS] Pre-building {len(self.COMMON)} phrases...")
        for phrase in self.COMMON:
            audio = self._generate(phrase)
            if audio is not None:
                self._cache[phrase.lower()] = audio
        logger.info("[TTS] Cache ready ? responses are now instant")

    def say(self, text: str):
        if not text:
            return
        self.speaking.set()
        try:
            audio = self._cache.get(text.lower())
            if audio is None:
                audio = self._generate(text)
                if audio is not None:
                    self._cache[text.lower()] = audio
            if audio is not None:
                duration = len(audio) / self.SR
                sd.stop()
                sd.play(audio, samplerate=self.SR, blocking=False)
                time.sleep(duration + 3.0)  # +3s echo tail ? room reverb suppression
        except Exception as e:
            logger.error(f"[TTS] say() error: {e}")
        finally:
            self.speaking.clear()

    def is_speaking(self) -> bool:
        return self.speaking.is_set()

    def beep(self):
        sr = 16000
        t = np.linspace(0, 0.1, int(sr * 0.1))
        b = (np.sin(2 * np.pi * 1000 * t) * 0.3).astype(np.float32)
        sound = np.concatenate([b, np.zeros(int(sr * 0.05), np.float32), b])
        try:
            sd.play(sound, samplerate=sr, blocking=False)
        except Exception:
            pass


# ?? Main ROS2 Node ????????????????????????????????????????????????????????????

class RestaurantAgentNode(Node):
    """
    ROS2 node: audio loop ? OpenWakeWord ? Sherpa-ONNX ? LangGraph ? Piper TTS
    """

    SLEEPING   = "sleeping"
    LISTENING  = "listening"
    PROCESSING = "processing"

    # Sherpa-ONNX chunk size = 80ms @ 16kHz (also matches OpenWakeWord)
    CHUNK = 1280

    def __init__(self):
        super().__init__("restaurant_agent")

        # ?? ROS Parameters ????????????????????????????????????????????????????
        self.declare_parameter("vosk_model_path",   _DEFAULT_VOSK_PATH)
        self.declare_parameter("sherpa_model_dir",  _DEFAULT_SHERPA_DIR)
        self.declare_parameter("hotwords_file",     _DEFAULT_HOTWORDS)
        self.declare_parameter("oww_model_paths",   _DEFAULT_OWW_PATHS)
        self.declare_parameter("piper_binary",      _DEFAULT_PIPER_BIN)
        self.declare_parameter("piper_model",       _DEFAULT_PIPER_MODEL)
        self.declare_parameter("sample_rate",       16000)
        self.declare_parameter("wake_timeout",      30.0)
        self.declare_parameter("table_id",          "")

        self._sr           = self.get_parameter("sample_rate").value
        self._wake_timeout = self.get_parameter("wake_timeout").value
        self._state_name   = self.SLEEPING
        self._state_lock   = threading.Lock()
        self._audio_q      = queue.Queue(maxsize=100)
        self._wake_timer   = None

        # ?? TTS ???????????????????????????????????????????????????????????????
        logger.info("[INIT] Starting Piper TTS...")
        self.tts = PiperTTS(
            self.get_parameter("piper_binary").value,
            self.get_parameter("piper_model").value,
        )

        # ?? STT Pipeline ??????????????????????????????????????????????????????
        logger.info("[INIT] Loading STT pipeline...")
        self._stt = self._build_stt_pipeline()

        # ?? LangGraph ?????????????????????????????????????????????????????????
        logger.info("[INIT] Building LangGraph agent...")
        self._graph = build_graph(ros_node=self)
        self._session: AgentState = default_state(
            table_id=self.get_parameter("table_id").value
        )

        # ?? ROS Publishers / Subscribers ??????????????????????????????????????
        self._pub_state   = self.create_publisher(String, "/robot/agent_state", 10)
        self._pub_cmd_vel = self.create_publisher(Twist, "/cmd_vel", 10)
        self.create_subscription(Float32, "/battery_level", self._on_battery, 10)

        # ?? Audio thread ??????????????????????????????????????????????????????
        threading.Thread(target=self._audio_thread, daemon=True).start()

        logger.info("[READY] Restaurant agent online")
        self.tts.beep()
        self.tts.say("Restaurant agent is ready")

    # ?? STT pipeline factory ??????????????????????????????????????????????????

    def _build_stt_pipeline(self) -> STTPipeline:
        vosk_path   = self.get_parameter("vosk_model_path").value
        sherpa_dir  = self.get_parameter("sherpa_model_dir").value
        hotwords    = self.get_parameter("hotwords_file").value
        oww_raw     = self.get_parameter("oww_model_paths").value
        oww_paths   = [p.strip() for p in oww_raw.split(",") if p.strip()] if oww_raw else []

        # Vosk wake detector ? grammar-constrained, reliable for "sonic"/"argo"
        wake = VoskWakeDetector(model_path=vosk_path, sample_rate=self._sr)

        # Sherpa-ONNX for full-vocabulary command recognition
        recognizer = StreamingRecognizer(
            model_dir=sherpa_dir,
            provider="cuda",
            rule1_silence=2.4,
            rule2_silence=1.2,
            hotwords_file=hotwords,
            hotwords_score=15.0,
        )

        # Optional: custom OWW model (future ? train with scripts/train_wake_word.py)
        oww = WakeWordDetector(model_paths=oww_paths if oww_paths else None)

        return STTPipeline(
            wake=wake,
            recognizer=recognizer,
            oww=oww,
            on_partial=lambda t: logger.debug(f"[STT] ? {t}"),
            vosk_model_path=vosk_path,
            sample_rate=self._sr,
        )

    # ?? State helpers ?????????????????????????????????????????????????????????

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
        self.tts.say("Going back to sleep")
        logger.info("[STATE] Sleeping (timeout)")

    # ?? ROS callbacks ?????????????????????????????????????????????????????????

    def _on_battery(self, msg: Float32):
        if msg.data < 15:
            logger.warning(f"[BATTERY] Low: {msg.data:.0f}%")
            self._session = {**self._session, "emergency_type": "low_battery"}
            threading.Thread(
                target=self._handle_utterance,
                args=("[low battery]",),
                daemon=True,
            ).start()

    # ?? Wake spin ?????????????????????????????????????????????????????????????

    def _wake_spin(self, degrees: float = 180.0, speed: float = 1.8):
        """
        Rotate in place by `degrees` at `speed` rad/s, then stop.
        Runs in a background thread so audio processing is not blocked.
        """
        def _spin():
            duration = math.radians(degrees) / speed
            msg = Twist()
            msg.angular.z = speed
            rate = 0.05  # 50 ms publish interval
            elapsed = 0.0
            while elapsed < duration:
                self._pub_cmd_vel.publish(msg)
                time.sleep(rate)
                elapsed += rate
            self._pub_cmd_vel.publish(Twist())  # stop

        threading.Thread(target=_spin, daemon=True).start()

    # ?? Audio thread ??????????????????????????????????????????????????????????

    def _audio_callback(self, indata, _frames, _time_info, status):
        if status:
            logger.debug(f"[AUDIO] {status}")
        try:
            self._audio_q.put_nowait(bytes(indata))
        except queue.Full:
            pass  # drop oldest if queue full (TTS playing)

    def _audio_thread(self):
        logger.info("[AUDIO] Thread started ? Sherpa-ONNX + OpenWakeWord")
        was_speaking = False

        with sd.RawInputStream(
            samplerate=self._sr,
            blocksize=self.CHUNK,
            dtype="int16",
            channels=1,
            callback=self._audio_callback,
        ):
            while rclpy.ok():
                try:
                    data = self._audio_q.get(timeout=0.5)
                    chunk = np.frombuffer(data, dtype=np.int16)

                    speaking = self.tts.is_speaking()

                    # TTS finished ? flush pipeline to drop echo
                    if was_speaking and not speaking:
                        was_speaking = False
                        # Drain queue
                        while not self._audio_q.empty():
                            try: self._audio_q.get_nowait()
                            except: break
                        self._stt.flush()
                        continue

                    if speaking:
                        was_speaking = True
                        continue

                    state = self._get_state()

                    # ?? SLEEPING: neural wake detection ???????????????????????
                    if state == self.SLEEPING:
                        woke, word = self._stt.process_sleeping(chunk)
                        if woke:
                            logger.info(f"[WAKE] Detected: '{word}'")
                            self._set_state(self.LISTENING)
                            self._stt.flush()
                            while not self._audio_q.empty():
                                try: self._audio_q.get_nowait()
                                except: break
                            self._wake_spin(degrees=180, speed=1.8)  # half tank turn
                            self.tts.beep()
                            # No TTS after wake ? just beep, then listen
                            self._reset_wake_timer()

                    # ?? LISTENING: streaming STT ??????????????????????????????
                    elif state == self.LISTENING:
                        final = self._stt.process_listening(chunk)
                        if final:
                            self._set_state(self.PROCESSING)
                            # Handle in a thread so audio thread stays responsive
                            threading.Thread(
                                target=self._handle_utterance,
                                args=(final,),
                                daemon=True,
                            ).start()

                except queue.Empty:
                    pass
                except Exception as e:
                    logger.error(f"[AUDIO] {e}", exc_info=True)

    # ?? LangGraph handler ?????????????????????????????????????????????????????

    def _handle_utterance(self, text: str):
        """Invoke LangGraph with recognised text, speak the response."""
        t0 = time.time()
        logger.info(f"[AGENT] Input: '{text}'")

        try:
            result = self._graph.invoke({**self._session, "user_input": text})
            self._session = result

            response    = result.get("response", "")
            intent      = result.get("current_intent", "")
            next_action = result.get("next_action", "")

            logger.info(
                f"[AGENT] intent={intent} next={next_action} "
                f"latency={time.time()-t0:.2f}s  response='{response[:60]}'"
            )

            # Drain stale audio queued during LLM call
            while not self._audio_q.empty():
                try: self._audio_q.get_nowait()
                except: break
            self._stt.flush()

            if response:
                self.tts.say(response)

            # Publish state snapshot
            self._pub_state.publish(String(data=json.dumps({
                "intent":       intent,
                "next_action":  next_action,
                "order_id":     self._session.get("order_id", ""),
                "order_status": self._session.get("order_status", ""),
                "latency_s":    round(time.time() - t0, 2),
            })))

            # Decide next state
            if next_action and next_action.startswith("await_"):
                # Mid-conversation ? stay awake, wait for next utterance
                self._reset_wake_timer()
                self._set_state(self.LISTENING)
            else:
                # Task complete ? go back to sleep
                if self._wake_timer:
                    self._wake_timer.cancel()
                self._set_state(self.SLEEPING)

        except Exception as e:
            logger.error(f"[AGENT] Error: {e}", exc_info=True)
            self.tts.say("Sorry, something went wrong. Please try again.")
            self._reset_wake_timer()
            self._set_state(self.LISTENING)


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
