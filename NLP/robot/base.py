from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional


@dataclass
class MoveResult:
    status: str                 # ok | blocked | no_sensors | error
    moved: float = 0.0
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "ok"


class Robot(ABC):
    @abstractmethod
    async def start(self) -> None: ...

    @abstractmethod
    async def close(self) -> None: ...

    @abstractmethod
    async def navigate(self, waypoint: dict) -> bool:
        """Drive to a waypoint ({x,y,qz,qw}); True on arrival. Cancelling the task cancels the goal."""

    @abstractmethod
    async def move(self, direction: str) -> MoveResult:
        """forward|backward|left|right (turn + 1 m)  or  spin|turn_left|turn_right (in place)."""

    @abstractmethod
    def clearance(self, direction: str) -> Optional[float]:
        """Free distance (m) ahead in that direction from all relevant sensors; None = no fresh data."""
