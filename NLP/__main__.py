from __future__ import annotations

import argparse
import asyncio
import logging

from .agent import run
from .config import load_settings


def main() -> None:
    ap = argparse.ArgumentParser(prog="python -m NLP", description="Sonic restaurant-robot voice agent")
    ap.add_argument("--text-mode", action="store_true", help="type instead of speak (no mic/speaker/Sarvam)")
    ap.add_argument("--sim", action="store_true", help="simulated robot (no ROS)")
    ap.add_argument("--wait-wake", action="store_true", help="text-mode: require typing 'hi sonic' to wake")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-5s %(name)s: %(message)s", datefmt="%H:%M:%S")
    for noisy in ("httpx", "httpcore", "websockets", "openai"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    s = load_settings()
    if not s.openai_api_key:
        raise SystemExit("OPENAI_API_KEY missing (NLP/.env or sonic/.env)")

    from .voice import SarvamVoice, TextVoice
    voice = TextVoice(s, auto_wake=not a.wait_wake) if a.text_mode else SarvamVoice(s)
    if a.sim:
        from .robot.mock import MockRobot
        robot = MockRobot()
    else:
        from .robot.ros import RosRobot
        robot = RosRobot(s)
    try:
        asyncio.run(run(s, voice, robot))
    except (KeyboardInterrupt, EOFError):
        pass


if __name__ == "__main__":
    main()
