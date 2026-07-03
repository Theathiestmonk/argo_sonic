"""
config.py
---------
Central config for Sonic — voice agent with real wake word + live mic STT.
Fill in your keys below. Nothing is hardcoded anywhere else.
"""

import os
from dotenv import load_dotenv

load_dotenv()

# ── Groq (LLM) ──────────────────────────────────────────────────────────────
GROQ_API_KEY     = os.environ.get("GROQ_API_KEY")   # free at console.groq.com
GROQ_URL         = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL       = "llama-3.1-8b-instant"
GROQ_TEMPERATURE = 0.72
GROQ_MAX_TOKENS  = 180

# ── Sarvam STT ───────────────────────────────────────────────────────────────
SARVAM_STT_URL   = "https://api.sarvam.ai/speech-to-text"
SARVAM_STT_MODEL = "saarika:v2.5"

# ── Sarvam TTS ───────────────────────────────────────────────────────────────
# IMPORTANT: regenerate your Sarvam key if you ever shared it publicly.
SARVAM_API_KEY     = os.environ.get("SARVAM_API_KEY")
SARVAM_TTS_URL     = "https://api.sarvam.ai/text-to-speech"
SARVAM_TTS_MODEL   = "bulbul:v3"
SARVAM_SPEAKER     = "shubh"       # neutral friendly male, bulbul:v3 default
SARVAM_LANGUAGE    = "en-IN"
SARVAM_PACE        = 1.1           # 0.5–2.0
SARVAM_TEMPERATURE = 0.6           # 0.01–1.0, v3 only
SARVAM_SAMPLE_RATE = 24000         # v3 default
SARVAM_TIMEOUT     = 15
# pitch/loudness are bulbul:v2 only — NOT sent for v3

# ── Wake word ────────────────────────────────────────────────────────────────
WAKE_WORD_MODEL_PATH = "models/Hi_Sonic.onnx"
WAKE_WORD_THRESHOLD  = 0.5     # lower = more sensitive, higher = fewer false triggers

# ── Microphone timing ────────────────────────────────────────────────────────
MIC_SILENCE_THRESHOLD = 500.0  # RMS volume below this counts as silence
                                # raise for noisy rooms, lower for quiet rooms
MIC_SILENCE_DURATION  = 1.5   # seconds of silence before recording stops
MIC_MAX_DURATION      = 12.0  # hard cap — never hangs longer than this

# How long to wait for the user to START speaking after wake word fires.
# If nobody starts within this window → say "I didn't catch that" and
# go back to wake-word mode immediately instead of waiting 12 seconds.
MIC_WAKE_WINDOW       = 4.0   # seconds

# How long to wait for a FOLLOW-UP after Sonic finishes responding.
# If the user doesn't say anything within this window → go back to
# wake-word mode and wait for "Hey Sonic" again.
MIC_FOLLOW_UP_WINDOW  = 5.0   # seconds  ← the fix you requested

# ── Scenarios ────────────────────────────────────────────────────────────────
# These are the selectable modes. "general" is always active on top of
# whichever scenario is selected — it's never a standalone session mode.
AVAILABLE_SCENARIOS = ["restaurant", "hotel", "bar", "home"]

# What Sonic says when asking which scenario to activate at startup
SCENARIO_PROMPT = (
    "Hello! I'm Sonic, your robot assistant. "
    "Before we start — am I working in a restaurant, hotel, bar, or home today?"
)

# Sonic's spoken response when a valid scenario is selected
SCENARIO_CONFIRMED = {
    "restaurant": "Got it — restaurant mode activated. I'm ready to help your guests!",
    "hotel":      "Got it — hotel mode activated. I'm ready to assist your guests!",
    "bar":        "Got it — bar mode activated. Ready to take drink orders!",
    "home":       "Got it — home mode activated. I'm here whenever you need me!",
}

# Keywords that map spoken input to a scenario selection
SCENARIO_KEYWORDS = {
    "restaurant": ["restaurant", "cafe", "dining", "food"],
    "hotel":      ["hotel", "reception", "hospitality", "resort", "rooms"],
    "bar":        ["bar", "pub", "drinks", "nightclub", "lounge"],
    "home":       ["home", "house", "smart home", "apartment", "flat"],
}

# ── LLM system prompt ────────────────────────────────────────────────────────
# Scenario context is injected at runtime — see llm_voice.build_system_prompt()
SONIC_BASE_PROMPT = """You are Sonic, a friendly two-wheeled carrier robot assistant.
You have no arms, two self-balancing wheels, LiDAR sensors, a camera, and a cargo platform.
Keep replies to 1-2 short sentences. Use contractions. Be warm and occasionally a little witty,
but never joke during emergencies or complaints. Speak naturally — never say "As an AI...".

IMPORTANT — always start your reply with a short 2-4 word opener sentence such as
"On it!", "Sure thing.", "Got it.", "Let me check.", or "Right away!" — this lets
Sonic start speaking almost instantly while the rest of the reply streams in.

{scenario_context}

CRITICAL RULE: If the user's request matches a known command for your current scenario,
do NOT invent a different answer. Acknowledge their request naturally and let the system
handle the action. Never override fixed command responses with your own wording."""
