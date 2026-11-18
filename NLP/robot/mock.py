"""Simulated robot for development without ROS/hardware.
NLP_MOCK_BLOCKED=forward,left  makes those directions report an obstacle."""
from __future__ import annotations

import asyncio
import os
from typing import Optional

from .base import MoveResult, Robot


class MockRobot(Robot):
    def __init__(self, nav_seconds: float = 1.0):
        self.nav_seconds = nav_seconds
        self.blocked = {d for d in os.getenv("NLP_MOCK_BLOCKED", "").split(",") if d}
        self.log: list[str] = []

    async def start(self) -> None: ...
    async def close(self) -> None: ...

    async def navigate(self, waypoint: dict) -> bool:
        self.log.append(f"navigate {waypoint.get('name')}")
        await asyncio.sleep(self.nav_seconds)
        return True

    def clearance(self, direction: str) -> Optional[float]:
        d = {"turn_left": "left", "turn_right": "right"}.get(direction, direction)
        return 0.3 if d in self.blocked else 5.0

    async def move(self, direction: str) -> MoveResult:
        if direction in self.blocked:
            return MoveResult("blocked")
        self.log.append(f"move {direction}")
        await asyncio.sleep(0.3)
        return MoveResult("ok", 1.0)
