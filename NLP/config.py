"""Runtime settings. Everything is env-overridable; defaults match this repo's layout."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

NLP_DIR = Path(__file__).resolve().parent
REPO_ROOT = NLP_DIR.parent

# NLP/.env wins, then the existing sonic/.env (already holds OPENAI_API_KEY / SARVAM_API_KEY).
load_dotenv(NLP_DIR / ".env")
load_dotenv(REPO_ROOT / "sonic" / ".env")


def _f(name: str, default: float) -> float:
    return float(os.getenv(name, default))


def _s(name: str, default: str) -> str:
    return os.getenv(name, default)


@dataclass(frozen=True)
class Settings:
    # --- LLM (classification + streamed answers). Smallest/fastest OpenAI model by default.
    openai_api_key: str = field(default_factory=lambda: _s("OPENAI_API_KEY", ""))
    llm_model: str = field(default_factory=lambda: _s("NLP_LLM_MODEL", "gpt-5.4-nano"))
    llm_fallbacks: tuple = field(default_factory=lambda: tuple(x for x in _s("NLP_LLM_FALLBACKS", "gpt-5-nano,gpt-4o-mini").split(",") if x))
    llm_timeout_s: float = field(default_factory=lambda: _f("NLP_LLM_TIMEOUT", 8.0))

    # --- Sarvam speech
    sarvam_api_key: str = field(default_factory=lambda: _s("SARVAM_API_KEY", ""))
    stt_model: str = field(default_factory=lambda: _s("SARVAM_STT_MODEL", "saaras:v3"))
    stt_mode: str = field(default_factory=lambda: _s("SARVAM_STT_MODE", "transcribe"))
    stt_language: str = field(default_factory=lambda: _s("SARVAM_STT_LANG", "en-IN"))
    tts_model: str = field(default_factory=lambda: _s("SARVAM_TTS_MODEL", "bulbul:v3"))
    tts_speaker: str = field(default_factory=lambda: _s("SARVAM_TTS_SPEAKER", "priya"))
    tts_language: str = field(default_factory=lambda: _s("SARVAM_TTS_LANG", "en-IN"))
    tts_pace: float = field(default_factory=lambda: _f("SARVAM_TTS_PACE", 1.1))
    tts_sample_rate: int = 22050

    # --- Audio / wake word
    wake_model: str = field(default_factory=lambda: _s("NLP_WAKE_MODEL", str(REPO_ROOT / "sonic" / "Hi_Sonic.onnx")))
    wake_threshold: float = field(default_factory=lambda: _f("NLP_WAKE_THRESHOLD", 0.5))
    mic_device: str = field(default_factory=lambda: _s("NLP_MIC_DEVICE", ""))
    speaker_device: str = field(default_factory=lambda: _s("NLP_SPEAKER_DEVICE", ""))
    vad_aggressiveness: int = 2
    end_silence_ms: int = field(default_factory=lambda: int(_f("NLP_END_SILENCE_MS", 550)))
    listen_timeout_s: float = field(default_factory=lambda: _f("NLP_LISTEN_TIMEOUT", 15.0))
    max_utterance_s: float = 12.0

    # --- Knowledge
    launcher_status_url: str = field(default_factory=lambda: _s("NLP_LAUNCHER_STATUS_URL", "http://127.0.0.1:8888/status"))
    map_from_launcher: bool = field(default_factory=lambda: _s("NLP_MAP_FROM_LAUNCHER", "1") == "1")  # follow the map Nav is running
    map_name: str = field(default_factory=lambda: _s("MAP_NAME", "office_map"))
    waypoints_dir: Path = field(default_factory=lambda: Path(_s("NLP_WAYPOINTS_DIR", str(REPO_ROOT / "src/argo_mini/waypoints"))))
    menu_file: Path = field(default_factory=lambda: Path(_s("NLP_MENU_FILE", str(REPO_ROOT / "src/argo_mini/menu/menu.json"))))
    menu_url: str = field(default_factory=lambda: _s("NLP_MENU_URL", "http://127.0.0.1:8888/menu"))
    restaurant_file: Path = field(default_factory=lambda: Path(_s("NLP_RESTAURANT_FILE", str(NLP_DIR / "data/restaurant.json"))))
    orders_dir: Path = field(default_factory=lambda: Path(_s("NLP_ORDERS_DIR", str(REPO_ROOT / "src/argo_mini/orders"))))
    cache_dir: Path = NLP_DIR / ".cache"
    database_url: str = field(default_factory=lambda: _s("DATABASE_URL", ""))
    robot_uid: str = field(default_factory=lambda: _s("ROBOT_UID", ""))
    pos_api_url: str = field(default_factory=lambda: _s("NLP_POS_API_URL", "http://127.0.0.1:8890"))   # backend/pos_api.py (order ledger)
    order_sink: str = field(default_factory=lambda: _s("NLP_ORDER_SINK", "auto"))  # auto|postgres|file

    # --- Motion (all go through /cmd_vel_smoothed -> safety shield -> /cmd_vel)
    move_distance_m: float = field(default_factory=lambda: _f("NLP_MOVE_DIST", 1.0))
    move_speed: float = field(default_factory=lambda: _f("NLP_MOVE_SPEED", 0.20))
    turn_speed: float = field(default_factory=lambda: _f("NLP_TURN_SPEED", 0.6))
    clearance_margin_m: float = field(default_factory=lambda: _f("NLP_CLEARANCE_MARGIN", 0.5))
    robot_radius_m: float = field(default_factory=lambda: _f("NLP_ROBOT_RADIUS", 0.30))
    stop_dist_m: float = field(default_factory=lambda: _f("NLP_STOP_DIST", 0.45))   # ≈ safety-shield lidar hard stop (0.5)
    corridor_half_width_m: float = field(default_factory=lambda: _f("NLP_CORRIDOR_HALF", 0.35))
    lidar_yaw_offset_rad: float = field(default_factory=lambda: _f("NLP_LIDAR_YAW_OFFSET", 0.0))
    cmd_topic: str = field(default_factory=lambda: _s("NLP_CMD_TOPIC", "/cmd_vel_smoothed"))
    require_sensors: bool = field(default_factory=lambda: _s("NLP_REQUIRE_SENSORS", "1") == "1")
    nav_timeout_s: float = field(default_factory=lambda: _f("NLP_NAV_TIMEOUT", 300.0))


def load_settings() -> Settings:
    return Settings()
