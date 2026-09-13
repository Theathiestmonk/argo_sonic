"""
companion_agent.py — minimal nav+chat voice agent for Sonic (restaurant
service robot). Replaces main_agent.py as the script backend/launcher.py
spawns: order-taking has moved to a separate, dedicated desktop robot, so
this robot's job shrinks to three things — (1) navigate on request (tables,
kitchen, dock, ...), (2) answer menu/cafe/about-itself questions and make
suggestions, (3) general small talk/jokes. It deliberately has NO agentic
order-building behaviour (no cart, no multi-turn slot-filling, no Postgres
order writes) — if a guest tries to order through it, it warmly redirects
them to the ordering station instead (see COMPANION_PERSONA).

Because there's no multi-step flow to track (no "which slot is still
missing" state machine order-taking needed), this file has no LangGraph —
every turn is one understanding+response LLM call (understand_and_respond())
followed by a simple dispatch on the returned intent. Much smaller than
main_agent.py's ~2500 lines as a result; this is the whole agent.

Everything the robot says is LLM-generated at speak-time (render() analog
is folded directly into understand_and_respond()'s response_text — see
"Understanding + response" below) against a local Ollama model
(qwen2.5:1.5b by default, see LLM_URL/LLM_MODEL), same as main_agent.py —
two deliberate exceptions, both for reliability/latency rather than
personality: the wake-word greeting (WAKE_GREETINGS) and the arrival
announcement (ARRIVAL_LINES) are canned rotations, and the "on my way"
navigation confirmation is a small template with the real destination name
interpolated rather than trusted free-form LLM phrasing — a wrong or
mismatched destination name spoken here would be actively misleading, not
just a bit stiff.

Run modes (same three as main_agent.py):
    python companion_agent.py               real mic/speaker/wake-word loop
    python companion_agent.py --text-mode   typed input / printed output
    python companion_agent.py --test-mode   real mic/speaker/wake-word loop,
        but every navigate_and_wait() call is an instant no-op arrival
        instead of a real Nav2 trip. Combine with --text-mode for a fully
        offline test. Same effect as SONIC_SKIP_NAV=1.
    TABLE_NO=<n> set (SONIC_ACTION_HINT accepted but ignored — there's no
        order/bill/deliver distinction left to make here) -> one dispatched
        trip straight to that table (no Kitchen detour — see git history of
        main_agent.py's own fix for this), then an open conversation loop
        right there, same as if a guest had woken it up and named that
        table themselves.

Requires Ollama running locally with qwen2.5:1.5b pulled (default — see
LLM_URL/LLM_MODEL/LLM_API_KEY to point at OpenAI or another provider
instead), SARVAM_API_KEY for real voice mode, and DATABASE_URL (menu is
DB-only — run seed_db.py first). Reuses the SAME Postgres
sonic_dialogue_sessions/sonic_dialogue_turns tables main_agent.py used —
backend/launcher.py's /voice/transcript endpoint keeps working unchanged.
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import os
import queue
import random
import re
import shlex
import socket
import subprocess
import sys
import threading
import time
import wave
from datetime import datetime
from typing import Optional

import numpy as np
import psycopg2
import requests
import sounddevice as sd
from dotenv import load_dotenv

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

load_dotenv()

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

SARVAM_API_KEY = os.environ.get("SARVAM_API_KEY")

# Local Ollama by default — see main_agent.py's LLM_URL/LLM_MODEL comment
# for the concurrent-GPU-load benchmark this default is based on.
LLM_URL = os.environ.get("LLM_URL", "http://localhost:11434/v1/chat/completions")
LLM_MODEL = os.environ.get("LLM_MODEL", "qwen2.5:1.5b")
LLM_API_KEY = os.environ.get("LLM_API_KEY") or os.environ.get("OPENAI_API_KEY") or "ollama"
LLM_KEEP_ALIVE = os.environ.get("LLM_KEEP_ALIVE", "")

SARVAM_STT_URL = "https://api.sarvam.ai/speech-to-text"
SARVAM_TTS_URL = "https://api.sarvam.ai/text-to-speech"
SARVAM_STT_MODEL = "saaras:v3"
SARVAM_TTS_MODEL = "bulbul:v3"
SARVAM_LANGUAGE_CODE = "en-IN"
SARVAM_SPEAKER = "ishita"
SARVAM_TTS_PACE = 1.0
SARVAM_TTS_WS_URL = "wss://api.sarvam.ai/text-to-speech/ws"
SARVAM_TTS_SAMPLE_RATE = 22050

WAKE_WORD_MODEL_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "Hi_Sonic.onnx")
WAKE_WORD_NAME = "Hi_Sonic"
WAKE_WORD_THRESHOLD = 0.35

NAV_BRIDGE_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "nav_bridge.py")
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WAYPOINTS_DIR = os.path.join(REPO_ROOT, "src", "argo_mini", "waypoints")
NAV_TIMEOUT_S = 300.0

VOICE_PROGRESS_PATH = "/tmp/argo_voice_progress"  # same path/shape main_agent.py used — dashboard reads this either way

_ON_JETSON = os.path.exists("/etc/nv_tegra_release") or "argo" in socket.gethostname().lower()

SAMPLE_RATE = 16000
FRAME_MS = 30
FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS // 1000

SILENCE_ONSET_TIMEOUT_S = 30.0     # how long one listen attempt waits for speech to start
TRAILING_SILENCE_MS = 600
SPEECH_RMS_THRESHOLD = int(os.environ.get("SONIC_RMS_THRESHOLD", "1000"))

# Unlike main_agent.py's order-taking (which never gives up on an
# in-progress order — abandoning one silently was judged worse than
# waiting forever), an open-ended chat has no natural "done" signal from
# the conversation itself. This is that signal instead: after this long
# with no reply anywhere in the session, say a brief goodbye and go back
# to sleep rather than stand there listening indefinitely.
SESSION_IDLE_TIMEOUT_S = float(os.environ.get("SONIC_SESSION_IDLE_TIMEOUT_S", "45"))

# How long the robot can sit away from its home base with no interaction
# at all before it drives itself back (see maybe_return_to_home_base()).
# Checked only while idly waiting for the wake word — never interrupts an
# active conversation or an in-progress trip.
IDLE_RETURN_TIMEOUT_S = float(os.environ.get("SONIC_IDLE_RETURN_TIMEOUT_S", "300"))

TEXT_MODE = False
TRACE_ENABLED = os.environ.get("SONIC_TRACE", "1") != "0"


def trace(event: str, **fields) -> None:
    if not TRACE_ENABLED:
        return
    details = " ".join(f"{k}={v!r}" for k, v in fields.items())
    print(f"[trace] {event}" + (f" | {details}" if details else ""))


DATABASE_URL = os.environ.get("DATABASE_URL")
ROBOT_UID = os.environ.get("ROBOT_UID", "SONIC-001")
FORCED_LOCATION = os.environ.get("TABLE_NO") or None  # SONIC_ACTION_HINT accepted by launcher.py but unused here
MAP_NAME = os.environ.get("SONIC_MAP_NAME", "office_map")
SONIC_SKIP_NAV = os.environ.get("SONIC_SKIP_NAV") == "1"


def require_api_keys(need_sarvam: bool = True) -> None:
    missing = []
    if not LLM_API_KEY:
        missing.append("LLM_API_KEY (or OPENAI_API_KEY if LLM_URL points at OpenAI)")
    if need_sarvam and not SARVAM_API_KEY:
        missing.append("SARVAM_API_KEY (get one from https://www.sarvam.ai)")
    if missing:
        print("Missing required API key(s):")
        for m in missing:
            print(f"  - {m}")
        print("Add them to a .env file (copy .env.example) before running.")
        sys.exit(1)
    try:
        t0 = time.monotonic()
        llm.chat("You are a helpful assistant.", "Reply with one word: ready", temperature=0.0, max_tokens=5)
        print(f"[main] LLM ({LLM_MODEL}) reachable, {time.monotonic() - t0:.1f}s round trip")
    except Exception as e:
        print(f"[warn] couldn't reach the LLM at {LLM_URL} ({e}) — check LLM_API_KEY/network. "
              "Spoken lines will fall back to a canned apology until this resolves.")


# ---------------------------------------------------------------------------
# Conversation state — deliberately tiny: no order/cart, no per-slot
# tracking. Just enough to keep replies coherent turn to turn and to know
# where the robot physically is right now.
# ---------------------------------------------------------------------------

class ChatState:
    def __init__(self):
        self.conversation_history: list = []   # [{"role","text"}]
        self.db_session_id: Optional[str] = None


_current_state: Optional[ChatState] = None
# None means genuinely unknown — deliberately NOT defaulted to "Kitchen" or
# any other fixed guess (see guess_starting_location() below, called once
# at startup): claiming a specific place with no actual basis is worse than
# admitting uncertainty, since a guest asking "where are you?" would get a
# confidently wrong answer instead of an honest one. Updated for real after
# every successful nav (see go_to()).
ROBOT_LOCATION: Optional[str] = None
LAST_ACTIVITY_TS = time.monotonic()

# Same file argo_sonic_nav.py's save_last_pose()/seed_initial_pose() read
# and write on shutdown/startup — see guess_starting_location() below.
LAST_POSE_PATH = "/tmp/argo_last_pose.json"
# How close (metres) the last saved pose must be to a real waypoint to
# guess that's where the robot is — a save file existing doesn't mean the
# robot is actually AT a named place (it could be mid-corridor), and
# guessing the nearest one anyway when it's genuinely far away would be
# exactly the kind of confidently-wrong answer this is meant to avoid.
STARTING_LOCATION_MATCH_DIST = 1.0


def guess_starting_location(waypoints: dict) -> None:
    """Best-effort real guess at where the robot is on startup, from the
    pose argo_sonic_nav.py's cleanup() saved on the PREVIOUS shutdown (see
    LAST_POSE_PATH) — matched against real waypoint positions rather than
    assuming a fixed spot. Leaves ROBOT_LOCATION as None (unknown) if
    there's no saved pose, it's unreadable, or nothing real is close
    enough to it — never guesses a specific place without real basis."""
    global ROBOT_LOCATION
    try:
        with open(LAST_POSE_PATH) as f:
            saved = json.load(f)
        x, y = saved["x"], saved["y"]
    except (OSError, json.JSONDecodeError, KeyError):
        return

    best_name, best_dist = None, None
    for wp in waypoints.values():
        name = wp.get("name")
        if not name or wp.get("status", "available") != "available":
            continue
        dist = ((wp["x"] - x) ** 2 + (wp["y"] - y) ** 2) ** 0.5
        if best_dist is None or dist < best_dist:
            best_name, best_dist = name, dist

    if best_name is None:
        print("[main] no real waypoints to match the saved pose against — starting location unknown")
    elif best_dist <= STARTING_LOCATION_MATCH_DIST:
        ROBOT_LOCATION = best_name
        print(f"[main] guessed starting location: {best_name!r} ({best_dist:.2f}m from last saved pose)")
    else:
        print(f"[main] last saved pose is {best_dist:.2f}m from the nearest waypoint {best_name!r} "
              "— leaving starting location unknown")


def _touch_activity() -> None:
    global LAST_ACTIVITY_TS
    LAST_ACTIVITY_TS = time.monotonic()


# ---------------------------------------------------------------------------
# Postgres — session/turn logging only (no orders, no visits/service points;
# same tables main_agent.py used, so backend/launcher.py's /voice/transcript
# endpoint keeps working unchanged since it only ever filtered by robot_id).
# Degrades to no-op if DATABASE_URL isn't set.
# ---------------------------------------------------------------------------

DB_ROBOT_ID: Optional[str] = None
_db_conn = None


def db():
    global _db_conn
    if not DATABASE_URL:
        return None
    if _db_conn is None or _db_conn.closed:
        try:
            _db_conn = psycopg2.connect(DATABASE_URL)
            _db_conn.autocommit = True
        except Exception as e:
            print(f"[warn] DB connection failed: {e}")
            _db_conn = None
    return _db_conn


def resolve_robot() -> None:
    global DB_ROBOT_ID
    conn = db()
    if conn is None:
        return
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT robot_id FROM robots WHERE robot_uid = %s", (ROBOT_UID,))
            row = cur.fetchone()
        if row is None:
            print(f"[warn] No robot row for ROBOT_UID={ROBOT_UID!r} — DB logging disabled for this run.")
            return
        DB_ROBOT_ID = str(row[0])
        print(f"[db] Connected — robot_id={DB_ROBOT_ID}")
    except Exception as e:
        print(f"[warn] DB robot lookup failed: {e}")


def db_start_session(state: ChatState) -> None:
    conn = db()
    if conn is None or DB_ROBOT_ID is None or state.db_session_id:
        return
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO sonic_dialogue_sessions (robot_id) VALUES (%s) RETURNING session_id",
                (DB_ROBOT_ID,),
            )
            state.db_session_id = str(cur.fetchone()[0])
    except Exception as e:
        print(f"[warn] DB session start failed: {e}")


def db_end_session(state: ChatState) -> None:
    conn = db()
    if conn is None or not state.db_session_id:
        return
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE sonic_dialogue_sessions SET ended_at = now() WHERE session_id = %s",
                        (state.db_session_id,))
    except Exception as e:
        print(f"[warn] DB session end failed: {e}")


def db_log_turn(state: ChatState, role: str, text: str, *, intent: Optional[str] = None) -> None:
    conn = db()
    if conn is None or not state.db_session_id:
        return
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO sonic_dialogue_turns (session_id, role, text, detected_intent) VALUES (%s, %s, %s, %s)",
                (state.db_session_id, role, text, intent),
            )
    except Exception as e:
        print(f"[warn] DB turn log failed: {e}")


# ---------------------------------------------------------------------------
# Menu (Postgres, read-only — descriptive Q&A only, no item/order matching)
# ---------------------------------------------------------------------------

MENU_ITEMS: list = []
DB_LOCATION_ID: Optional[str] = None


def load_menu_from_db() -> None:
    global MENU_ITEMS, DB_LOCATION_ID
    conn = db()
    if conn is None:
        print("No database available — menu can't be loaded. Set DATABASE_URL, then run seed_db.py.")
        return
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT robot_id, location_id FROM robots WHERE robot_uid = %s", (ROBOT_UID,))
            row = cur.fetchone()
            if row is None:
                print(f"[warn] No robot row for ROBOT_UID={ROBOT_UID!r} — menu can't be loaded.")
                return
            DB_LOCATION_ID = str(row[1])
            cur.execute(
                """SELECT mi.item_name, mc.name, mi.price, mi.is_available, mi.description
                   FROM menu_items mi
                   LEFT JOIN menu_categories mc ON mi.category_id = mc.category_id
                   WHERE mi.location_id = %s""",
                (DB_LOCATION_ID,),
            )
            rows = cur.fetchall()
    except Exception as e:
        print(f"[warn] Menu load from DB failed: {e}")
        return

    MENU_ITEMS = [
        {"name": name, "category": (category or "all").strip().lower(),
         "price": float(price), "available": is_available, "description": description or ""}
        for name, category, price, is_available, description in rows
    ]
    print(f"[db] Loaded {len(MENU_ITEMS)} menu items from Postgres")


def menu_context_for_llm() -> str:
    if not MENU_ITEMS:
        return "(menu not loaded)"
    by_category: dict = {}
    for item in MENU_ITEMS:
        if not item["available"]:
            continue
        by_category.setdefault(item["category"], []).append(item)
    lines = []
    for category in sorted(by_category):
        lines.append(f"{category}:")
        for item in by_category[category]:
            desc = f" — {item['description']}" if item["description"] else ""
            lines.append(f'  - "{item["name"]}" (₹{item["price"]:.2f}){desc}')
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Waypoints (map JSON, read-only — same file nav_bridge.py's load_waypoint()
# resolves against, so a name the guest gives us that we accept here is
# guaranteed to actually resolve there too)
# ---------------------------------------------------------------------------

def load_waypoints(map_name: str = MAP_NAME) -> dict:
    path = os.path.join(WAYPOINTS_DIR, f"{map_name}.json")
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(f"[warn] couldn't load waypoints from {path}: {e}")
        return {}


def available_destination_names(waypoints: dict) -> list:
    return sorted({
        wp["name"] for wp in waypoints.values()
        if wp.get("name") and wp.get("status", "available") == "available"
    })


def resolve_home_base(names: list) -> Optional[str]:
    """Where maybe_return_to_home_base() drives back to when idle. A
    "dock"/"docking station"-named waypoint (however a given map happens to
    spell it — "Docker" in office_map.json, nothing in Atsn_cafe_map.json)
    is preferred over Kitchen since it's the actual charging spot when one
    exists; Kitchen is the sensible fallback either way. SONIC_HOME_BASE
    overrides both when set, e.g. for a map that names it something else
    entirely."""
    override = os.environ.get("SONIC_HOME_BASE")
    if override and override in names:
        return override
    for name in names:
        if "dock" in name.lower():
            return name
    if "Kitchen" in names:
        return "Kitchen"
    return names[0] if names else None


# ---------------------------------------------------------------------------
# Audio I/O — identical to main_agent.py's (same hardware)
# ---------------------------------------------------------------------------

def select_usb_audio_device() -> None:
    if not _ON_JETSON:
        return
    try:
        devices = sd.query_devices()
        in_idx = out_idx = None
        for i, dev in enumerate(devices):
            name = dev.get("name", "")
            if "USB PnP" not in name and "Device" not in name:
                continue
            if in_idx is None and dev.get("max_input_channels", 0) > 0:
                in_idx = i
            if out_idx is None and dev.get("max_output_channels", 0) > 0:
                out_idx = i
        if in_idx is None and out_idx is None:
            print("[warn] USB PnP audio device not found — falling back to sounddevice's system default")
            return
        sd.default.device = (in_idx, out_idx)
        in_name = devices[in_idx]["name"] if in_idx is not None else "(system default)"
        out_name = devices[out_idx]["name"] if out_idx is not None else "(system default)"
        print(f"[main] Audio device auto-selected — input: {in_name!r}, output: {out_name!r}")
    except Exception as e:
        print(f"[warn] USB audio device auto-select failed, using sounddevice's default: {e}")


def record_utterance(timeout_s: float = SILENCE_ONSET_TIMEOUT_S) -> Optional[np.ndarray]:
    q: queue.Queue = queue.Queue()

    def callback(indata, frames, time_info, status):
        q.put(indata.copy())

    frames_collected = []
    speech_started = False
    silence_run_ms = 0
    start = time.monotonic()

    with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="int16",
                         blocksize=FRAME_SAMPLES, callback=callback):
        while True:
            try:
                chunk = q.get(timeout=0.5)
            except queue.Empty:
                if not speech_started and (time.monotonic() - start) > timeout_s:
                    return None
                continue

            rms = float(np.sqrt(np.mean(chunk.astype(np.float64) ** 2)))

            if not speech_started:
                if rms > SPEECH_RMS_THRESHOLD:
                    speech_started = True
                    frames_collected.append(chunk)
                elif (time.monotonic() - start) > timeout_s:
                    return None
                continue

            frames_collected.append(chunk)
            if rms > SPEECH_RMS_THRESHOLD:
                silence_run_ms = 0
            else:
                silence_run_ms += FRAME_MS
                if silence_run_ms >= TRAILING_SILENCE_MS:
                    break

    return np.concatenate(frames_collected, axis=0).flatten()


def play_audio(audio: np.ndarray, samplerate: int = SAMPLE_RATE) -> None:
    sd.play(audio, samplerate=samplerate)
    sd.wait()


# ---------------------------------------------------------------------------
# Sarvam AI: STT (saaras:v3) / TTS (bulbul:v3) — identical to main_agent.py's
# ---------------------------------------------------------------------------

def sarvam_stt(audio: np.ndarray) -> str:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(audio.astype(np.int16).tobytes())
    buf.seek(0)

    resp = requests.post(
        SARVAM_STT_URL,
        headers={"api-subscription-key": SARVAM_API_KEY},
        data={"model": SARVAM_STT_MODEL, "language_code": SARVAM_LANGUAGE_CODE},
        files={"file": ("utterance.wav", buf, "audio/wav")},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    return (data.get("transcript") or data.get("text") or "").strip()


def sarvam_tts(text: str, language_code: str = SARVAM_LANGUAGE_CODE) -> tuple[np.ndarray, int]:
    resp = requests.post(
        SARVAM_TTS_URL,
        headers={"api-subscription-key": SARVAM_API_KEY, "Content-Type": "application/json"},
        json={
            "inputs": [text],
            "target_language_code": language_code,
            "model": SARVAM_TTS_MODEL,
            "speaker": SARVAM_SPEAKER,
            "pace": SARVAM_TTS_PACE,
        },
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    audios = data.get("audios") or []
    if not audios:
        raise RuntimeError(f"Sarvam TTS returned no audio: {data}")
    raw = base64.b64decode(audios[0])
    with wave.open(io.BytesIO(raw), "rb") as wf:
        sr = wf.getframerate()
        n_channels = wf.getnchannels()
        pcm = np.frombuffer(wf.readframes(wf.getnframes()), dtype=np.int16)
    if n_channels > 1:
        pcm = pcm.reshape(-1, n_channels).mean(axis=1).astype(np.int16)
    return pcm, sr


def sarvam_tts_stream(sentences: list, language_code: str = SARVAM_LANGUAGE_CODE) -> bool:
    try:
        import websockets.sync.client as ws_client
    except ImportError:
        return False

    played_any = False
    stream = None
    url = f"{SARVAM_TTS_WS_URL}?model={SARVAM_TTS_MODEL}&send_completion_event=true"
    try:
        with ws_client.connect(url, additional_headers={"Api-Subscription-Key": SARVAM_API_KEY}) as ws:
            ws.send(json.dumps({
                "type": "config",
                "data": {
                    "language_code": language_code,
                    "speaker": SARVAM_SPEAKER,
                    "model": SARVAM_TTS_MODEL,
                    "pace": SARVAM_TTS_PACE,
                    "speech_sample_rate": SARVAM_TTS_SAMPLE_RATE,
                    "output_audio_codec": "linear16",
                },
            }))
            for sentence in sentences:
                ws.send(json.dumps({"type": "text", "data": {"text": sentence}}))
                ws.send(json.dumps({"type": "flush"}))

            stream = sd.OutputStream(samplerate=SARVAM_TTS_SAMPLE_RATE, channels=1, dtype="int16")
            stream.start()

            pending = len(sentences)
            while pending > 0:
                msg = json.loads(ws.recv(timeout=20))
                mtype = msg.get("type")
                if mtype == "audio":
                    pcm = np.frombuffer(base64.b64decode(msg["data"]["audio"]), dtype=np.int16)
                    if pcm.size:
                        stream.write(pcm)
                        played_any = True
                elif mtype == "event" and msg.get("data", {}).get("event_type") == "final":
                    pending -= 1
                elif mtype == "error":
                    print(f"[warn] Sarvam streaming TTS error: {msg}")
                    return played_any
        return True
    except Exception as e:
        print(f"[warn] Sarvam streaming TTS {'ended early' if played_any else 'unavailable, falling back to REST'}: {e}")
        return played_any
    finally:
        if stream is not None:
            stream.stop()
            stream.close()


_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")


def split_sentences(text: str) -> list:
    parts = _SENTENCE_SPLIT_RE.split(text.strip())
    return [p for p in parts if p]


def speak_text(text: str, *, intent: Optional[str] = None) -> None:
    print(f"Sonic: {text}")
    if _current_state is not None:
        _current_state.conversation_history.append({"role": "robot", "text": text})
        db_log_turn(_current_state, "robot", text, intent=intent)
    if TEXT_MODE or not text:
        return

    sentences = split_sentences(text)
    if not sentences:
        return

    if sarvam_tts_stream(sentences):
        return

    audio_queue: "queue.Queue" = queue.Queue(maxsize=2)
    DONE = object()

    def synthesize_worker():
        for sentence in sentences:
            try:
                audio_queue.put(sarvam_tts(sentence))
            except Exception as e:
                print(f"[warn] TTS failed for a chunk: {e}")
        audio_queue.put(DONE)

    worker = threading.Thread(target=synthesize_worker, daemon=True)
    worker.start()

    while True:
        item = audio_queue.get()
        if item is DONE:
            break
        audio, sr = item
        try:
            play_audio(audio, sr)
        except Exception as e:
            print(f"[warn] audio playback failed: {e}")
    worker.join(timeout=1.0)


def listen(timeout_s: float = SILENCE_ONSET_TIMEOUT_S) -> Optional[str]:
    """Returns the transcript (possibly ""), or None specifically when no
    speech was ever detected within timeout_s — see main_agent.py's listen()
    for the same "" vs None distinction this mirrors."""
    if TEXT_MODE:
        try:
            text = input("You: ").strip()
        except EOFError:
            print()
            sys.exit(0)
        if text.lower() in ("quit", "exit"):
            print("Goodbye!")
            sys.exit(0)
        return text or None

    audio = record_utterance(timeout_s)
    if audio is None:
        return None
    try:
        transcript = sarvam_stt(audio)
    except Exception as e:
        print(f"[warn] STT failed: {e}")
        transcript = ""
    print(f"You said: {transcript}")
    return transcript


# ---------------------------------------------------------------------------
# Wake word (openWakeWord + Hi_Sonic.onnx) — identical to main_agent.py's
# ---------------------------------------------------------------------------

def load_wake_word_model():
    try:
        from openwakeword import Model
    except ImportError:
        from openwakeword.model import Model
    return Model(wakeword_models=[WAKE_WORD_MODEL_PATH], inference_framework="onnx")


def wait_for_wake_word(oww_model) -> None:
    """Blocks until the wake word fires, but polls with a timeout instead
    of blocking forever on the mic queue so maybe_return_to_home_base() can
    run periodically while idle — see the main_loop() call site."""
    oww_model.reset()
    q: queue.Queue = queue.Queue()

    def callback(indata, frames, time_info, status):
        q.put(indata.copy().flatten())

    with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="int16",
                         blocksize=1280, callback=callback):
        while True:
            try:
                chunk = q.get(timeout=1.0)
            except queue.Empty:
                maybe_return_to_home_base()
                continue
            predictions = oww_model.predict(chunk)
            score = predictions.get(WAKE_WORD_NAME)
            if score is None and predictions:
                score = next(iter(predictions.values()))
            if score is not None and score > WAKE_WORD_THRESHOLD:
                warm_up_llm()
                return


# ---------------------------------------------------------------------------
# Navigation — shells out to the existing, unmodified sonic/nav_bridge.py
# ---------------------------------------------------------------------------

def report_phase(phase: Optional[str], text: str = "", table: Optional[str] = None) -> None:
    try:
        with open(VOICE_PROGRESS_PATH, "w") as f:
            json.dump({"phase": phase, "text": text, "table": table,
                       "updated_at": datetime.now().isoformat()}, f)
    except OSError as e:
        print(f"[warn] report_phase write failed: {e}")


def navigate_and_wait(destination: str, timeout_s: float = NAV_TIMEOUT_S) -> bool:
    if SONIC_SKIP_NAV:
        print(f"[nav] test mode — treating {destination!r} as instantly reached, not actually driving")
        return True

    cmd = (
        "source /opt/ros/humble/setup.bash && "
        f"source {REPO_ROOT}/install/setup.bash && "
        "export RMW_IMPLEMENTATION=rmw_cyclonedds_cpp && "
        f"python3 {shlex.quote(NAV_BRIDGE_SCRIPT)} --map {shlex.quote(MAP_NAME)} "
        f"--destination {shlex.quote(destination)} --timeout {timeout_s}"
    )
    print(f"[nav] -> {destination!r} (timeout {timeout_s:.0f}s)")
    try:
        result = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True, timeout=timeout_s + 15)
    except subprocess.TimeoutExpired:
        print(f"[nav] {destination!r}: bridge process itself timed out")
        return False

    ok = result.returncode == 0 and "RESULT:SUCCESS" in result.stdout
    print(f"[nav] {destination!r}: {'arrived' if ok else 'FAILED'} (rc={result.returncode})")
    if not ok:
        if result.stdout:
            print(f"[nav] stdout: {result.stdout.strip()[-1000:]}")
        if result.stderr:
            print(f"[nav] stderr: {result.stderr.strip()[-500:]}")
    return ok


def go_to(destination: str, *, announce: str) -> bool:
    """Speaks `announce` while the trip starts in parallel, same
    overlap-not-sequence pattern main_agent.py's perform_travel_action()
    used — the wheels shouldn't sit still through the whole announcement
    line before actually moving."""
    global ROBOT_LOCATION
    report_phase("heading_to_table", f"Heading to {destination}", destination)
    result: list[bool] = []
    nav_thread = threading.Thread(target=lambda: result.append(navigate_and_wait(destination)))
    nav_thread.start()
    speak_text(announce, intent="navigate")
    nav_thread.join()
    arrived = result[0] if result else False
    if arrived:
        ROBOT_LOCATION = destination
        report_phase("arrived", f"Arrived at {destination}", destination)
    else:
        report_phase(None)
    return arrived


def maybe_return_to_home_base() -> None:
    """Called only while idly waiting for the wake word (see
    wait_for_wake_word()) — never during an active conversation or trip.
    Drives itself back to HOME_BASE if it's been sitting elsewhere, unvisited,
    for IDLE_RETURN_TIMEOUT_S. Silently does nothing if no home base could
    be resolved for the current map (see resolve_home_base()). ROBOT_LOCATION
    being None (unknown — see guess_starting_location()) doesn't skip this:
    None != HOME_BASE either way, so an unknown location still drives home
    rather than sitting still on the assumption it might already be there —
    a real trip from wherever it actually is (Nav2 doesn't care what this
    Python string says) resolves the unknown either way once it arrives."""
    if HOME_BASE is None or ROBOT_LOCATION == HOME_BASE:
        return
    if time.monotonic() - LAST_ACTIVITY_TS < IDLE_RETURN_TIMEOUT_S:
        return
    print(f"[idle] no activity for {IDLE_RETURN_TIMEOUT_S:.0f}s — returning to {HOME_BASE!r}")
    _touch_activity()  # reset the window immediately so a failed attempt doesn't retry-loop every second
    go_to(HOME_BASE, announce=f"Heading back to {HOME_BASE} while I wait.")


# ---------------------------------------------------------------------------
# LLM layer
# ---------------------------------------------------------------------------

class GroqJSONError(RuntimeError):
    def __init__(self, message: str, failed_generation: Optional[str] = None):
        super().__init__(message)
        self.failed_generation = failed_generation


class LLMClient:
    def __init__(self, base_url: str = LLM_URL, model: str = LLM_MODEL, api_key: Optional[str] = None,
                 keep_alive: str = LLM_KEEP_ALIVE):
        self.base_url = base_url
        self.model = model
        self.api_key = api_key or LLM_API_KEY
        self.keep_alive = keep_alive

    def chat(self, system: str, user: str, *, json_mode: bool = False, temperature: float = 0.2,
              max_tokens: Optional[int] = None) -> str:
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        payload = {"model": self.model, "messages": messages, "temperature": temperature}
        if max_tokens:
            payload["max_tokens"] = max_tokens
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        if self.keep_alive:
            payload["keep_alive"] = self.keep_alive

        last_err = None
        for attempt in range(2):
            payload["temperature"] = temperature + (0.15 if attempt > 0 else 0.0)
            try:
                resp = requests.post(
                    self.base_url,
                    headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                    json=payload, timeout=30,
                )
                if not resp.ok:
                    raise RuntimeError(f"{resp.status_code} {resp.reason}: {resp.text[:500]}")
                return resp.json()["choices"][0]["message"]["content"]
            except Exception as e:
                last_err = e
                time.sleep(0.5)
        raise RuntimeError(f"LLM call failed after retries: {last_err}")


llm = LLMClient()

WAKE_GREETINGS = ["Hi!", "Hello!", "Yes?", "I'm here!", "I'm awake!"]
ARRIVAL_LINES = ["Here I am!", "Made it!", "I've arrived!", "Here you go!"]


def warm_up_llm() -> None:
    """Fire-and-forget on wake-word detection — see main_agent.py's
    warm_up_llm() for the full rationale (same design, copied here)."""
    def _run():
        try:
            t0 = time.monotonic()
            llm.chat("You are ready.", "Reply with one word.", temperature=0.0, max_tokens=1)
            trace("llm_warm_up_done", elapsed_s=round(time.monotonic() - t0, 2))
        except Exception as e:
            print(f"[warn] LLM warm-up failed (first real call will retry): {e}")

    threading.Thread(target=_run, daemon=True).start()


COMPANION_PERSONA = (
    "You are Sonic, a warm and friendly restaurant/cafe host-and-navigation robot. You are female — if it "
    "ever comes up (the guest asks, or you'd naturally refer to yourself), use she/her, never he/him. Sound "
    "like a chatty, genuinely warm host, not a script. Speak natural Indian English — the guest is in India, "
    "so phrase things the way an Indian host would, not American/British English. Keep replies to 1-2 short "
    "spoken sentences. Never wrap your reply in quotation marks — plain spoken text only, and never use "
    "markdown formatting (no asterisks, no bullet points) since this is read aloud, not displayed as text. "
    "Never invent menu items, prices, or facts that aren't given to you in the context below.\n\n"
    "IMPORTANT — what you can and can't do: you do NOT take food orders yourself. Ordering happens at a "
    "separate ordering station/tablet at each table. If a guest tries to order food through you, warmly let "
    "them know you can't take the order yourself but the ordering station can help, without being preachy "
    "about it. You CAN: drive to a table/the kitchen/the dock when asked, describe what's on the menu and "
    "make suggestions, talk about the cafe and about yourself, tell a joke, and make small talk."
)

INTENT_SCHEMA = """{
  "intent": "<navigate, menu, order_attempt, call_staff, status, about, joke, small_talk, or unclear>",
  "destination": "<ONLY for intent=navigate: the EXACT destination name from the list below, copied character-for-character, or null if you can't tell which real place they mean>"
}"""


def build_classify_prompt(destinations: list) -> str:
    return f"""Classify the guest's latest utterance to a restaurant host-and-navigation robot. Return ONLY \
a JSON object (no prose, no markdown fences) with exactly this shape:

{INTENT_SCHEMA}

Real places the robot can navigate to:
{", ".join(destinations) or "(none available)"}

Guidance:
- navigate: any request to go/come/drive somewhere (a table, the kitchen, the dock, ...). destination must be \
copied EXACTLY from the real places list above, or null if it's not one of them or you can't tell which they \
mean — never guess a paraphrase.
- order_attempt: they're trying to order food/drinks (naming a dish, "I'll have...", "can I get...", asking \
you to bring food) — this is about THEM wanting to order, not asking what's available (that's menu).
- menu: asking what's available, prices, ingredients, or wanting a recommendation/suggestion.
- status: asking where you are, what you're doing, or whether you're coming.
- about: asking about the cafe/restaurant, or about the robot itself (who/what are you, are you a robot).
- joke: explicitly asking for a joke or something funny.
- small_talk: general chit-chat not covered above (greetings, how are you, etc.).
- call_staff: bill/complaints/anything needing a human, or you genuinely can't tell what they want at all.
- Return valid JSON only, no other text.
"""


def classify_intent(transcript: str, destinations: list) -> dict:
    system = build_classify_prompt(destinations)
    result = {}
    try:
        raw = llm.chat(system, transcript, json_mode=True, temperature=0.1)
        result = json.loads(raw)
    except Exception as e:
        print(f"[warn] intent classification call/parse failed: {e}")

    destination = result.get("destination") or None
    if destination and destination.strip() not in destinations:
        print(f"[warn] LLM destination {destination!r} isn't a real waypoint — treating as no match")
        destination = None

    intent = result.get("intent") or "unclear"
    trace("classify", transcript=transcript, intent=intent, destination=destination)
    return {"intent": intent, "destination": destination}


# Per-intent instruction for the SEPARATE response-generation call — same
# split main_agent.py uses (extract_slots() vs render()), applied here after
# testing showed one combined classify+respond call unreliable at this model
# size: it dropped the order-redirect instruction entirely, refused to tell
# a joke, and even misclassified a plain "go to the kitchen" as call_staff.
# Isolating "what to say" from "what JSON to produce" fixed all three, same
# lesson tonight's menu-suggestion work already learned the hard way.
RESPONSE_HINTS = {
    "navigate_ok": "Give a brief, warm acknowledgment that you're heading to {destination} now.",
    "navigate_unknown": "You couldn't tell which real place they meant. Apologize briefly and ask them to "
                         "say the table/place again.",
    "order_attempt": "They just tried to order food or drinks through you. Warmly explain you can't take "
                      "orders yourself, but the ordering station at their table can help with that. Do NOT "
                      "confirm, repeat back, or acknowledge any specific item they named — just redirect.",
    "menu": "Answer their menu question or give a suggestion naturally from the real menu below, like a host "
            "describing what's on offer — not reading out a full price list unless they ask for that.",
    "status": "Tell them where you currently are and, briefly, what you're doing, naturally.",
    "about": "Answer warmly and briefly — about the cafe/restaurant, or about yourself if they're asking who "
              "you are.",
    "joke": "Tell one short, genuinely funny, family-friendly joke.",
    "small_talk": "Respond warmly and briefly to the small talk/chit-chat.",
    "call_staff": "Warmly let them know you'll get a staff member to help with that.",
    "unclear": "You couldn't understand what they want. Ask them to repeat or rephrase, briefly.",
}


def build_response_prompt(purpose: str, state: ChatState, **ctx) -> str:
    hint = RESPONSE_HINTS.get(purpose, purpose)
    try:
        hint = hint.format(**ctx)
    except (KeyError, IndexError):
        pass
    history_tail = state.conversation_history[-6:]
    history_text = "\n".join(f"{t['role']}: {t['text']}" for t in history_tail) or "(nothing yet)"
    menu_block = f"\n\nThe real menu (use this — and ONLY this; never invent an item):\n{menu_context_for_llm()}" \
        if purpose == "menu" else ""
    if purpose == "status":
        location_text = ROBOT_LOCATION or "an unconfirmed spot — you're not sure exactly where, but you're fine and listening"
        location_block = f"\n\nYou are currently at: {location_text}"
    else:
        location_block = ""
    return f"Recent conversation:\n{history_text}{location_block}{menu_block}\n\nWhat to say now: {hint}"


def clean_spoken_text(text: Optional[str]) -> str:
    if not text:
        return ""
    cleaned = text.strip().strip('"\'“”‘’').strip()
    if cleaned and cleaned[0].isalpha():
        cleaned = cleaned[0].upper() + cleaned[1:]
    return cleaned


def render_response(purpose: str, state: ChatState, **ctx) -> str:
    user_prompt = build_response_prompt(purpose, state, **ctx)
    try:
        text = llm.chat(COMPANION_PERSONA, user_prompt, temperature=0.6, max_tokens=120)
    except Exception as e:
        print(f"[warn] render_response failed for purpose={purpose!r}: {e}")
        return "Sorry, I'm having a little trouble right now — one moment."
    return clean_spoken_text(text) or "Sorry, one moment."


# ---------------------------------------------------------------------------
# Turn handling / conversation loop
# ---------------------------------------------------------------------------

def handle_navigate(state: ChatState, destination: Optional[str]) -> None:
    if not destination:
        speak_text(render_response("navigate_unknown", state), intent="navigate")
        return
    announce = render_response("navigate_ok", state, destination=destination)
    arrived = go_to(destination, announce=announce)
    if arrived:
        speak_text(random.choice(ARRIVAL_LINES), intent="navigate")
    else:
        speak_text("Sorry, I'm having trouble getting there right now — let me get a staff member to help.",
                    intent="navigate")


def handle_turn(state: ChatState, transcript: str, destinations: list) -> None:
    _touch_activity()
    state.conversation_history.append({"role": "user", "text": transcript})
    classified = classify_intent(transcript, destinations)
    intent = classified["intent"]
    if intent == "navigate":
        handle_navigate(state, classified["destination"])
    else:
        speak_text(render_response(intent, state), intent=intent)


def run_session(initial_table: Optional[str] = None) -> None:
    """One wake -> conversation -> idle-timeout cycle. initial_table skips
    the wake-word greeting and goes straight into a normal turn loop at
    that table (dashboard dispatch — see main())."""
    global _current_state
    state = ChatState()
    _current_state = state
    db_start_session(state)
    _touch_activity()

    waypoints = load_waypoints(MAP_NAME)
    destinations = available_destination_names(waypoints)

    if initial_table:
        if initial_table in destinations:
            handle_navigate(state, initial_table)
        else:
            print(f"[warn] TABLE_NO={initial_table!r} isn't a real waypoint on map {MAP_NAME!r} — "
                  "starting the conversation without moving.")
            speak_text(random.choice(WAKE_GREETINGS))
    else:
        speak_text(random.choice(WAKE_GREETINGS))

    try:
        while True:
            transcript = listen(timeout_s=SESSION_IDLE_TIMEOUT_S)
            if transcript is None:
                print("[session] idle timeout — going back to sleep")
                return
            if not transcript:
                continue
            handle_turn(state, transcript, destinations)
    finally:
        db_end_session(state)
        report_phase(None)
        _current_state = None


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

HOME_BASE: Optional[str] = None


def main_loop() -> None:
    global HOME_BASE
    waypoints = load_waypoints(MAP_NAME)
    HOME_BASE = resolve_home_base(available_destination_names(waypoints))
    print(f"[main] home base: {HOME_BASE!r}" if HOME_BASE else "[main] no home base resolved — idle-return disabled")
    guess_starting_location(waypoints)

    if TEXT_MODE:
        print("=== companion_agent.py (text mode) — no mic, no wake word, no Sarvam calls ===")
        print("Type your first utterance directly (the greeting is simulated).")
        print("Press Enter on a blank line to simulate silence/timeout. Type 'quit' to exit.\n")
        while True:
            run_session(initial_table=FORCED_LOCATION)
    elif FORCED_LOCATION:
        require_api_keys()
        print(f"companion_agent dispatched: table={FORCED_LOCATION!r} (no wake word needed).")
        run_session(initial_table=FORCED_LOCATION)
    else:
        require_api_keys()
        oww_model = load_wake_word_model()
        print(f"companion_agent is listening for the wake word ('{WAKE_WORD_NAME}')...")
        while True:
            wait_for_wake_word(oww_model)
            run_session()


def main() -> None:
    global TEXT_MODE, SONIC_SKIP_NAV
    parser = argparse.ArgumentParser(description="Sonic companion_agent — minimal nav+chat voice agent")
    parser.add_argument("--text-mode", action="store_true",
                        help="Run with typed input/printed output instead of mic/speaker/wake-word")
    parser.add_argument("--test-mode", action="store_true",
                        help="Skip real Nav2 trips (instant-arrival) — everything else stays real. "
                             "Same effect as SONIC_SKIP_NAV=1.")
    args = parser.parse_args()
    TEXT_MODE = args.text_mode
    SONIC_SKIP_NAV = SONIC_SKIP_NAV or args.test_mode

    if TEXT_MODE:
        require_api_keys(need_sarvam=False)
    else:
        select_usb_audio_device()
    if SONIC_SKIP_NAV:
        print("[main] test mode — navigation will be skipped (instant arrival), everything else is real")
    print(f"Node-execution tracing: {'ON' if TRACE_ENABLED else 'OFF'} (set SONIC_TRACE=0 to disable)")
    resolve_robot()
    load_menu_from_db()
    if FORCED_LOCATION:
        print(f"[db] TABLE_NO={FORCED_LOCATION!r} set — session will skip the wake-word greeting")
    main_loop()


if __name__ == "__main__":
    main()
