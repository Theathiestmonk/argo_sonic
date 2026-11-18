from __future__ import annotations

from dataclasses import dataclass

from ..config import Settings
from ..knowledge import Menu, Restaurant, Waypoints
from ..llm import LLM
from ..robot.base import Robot
from ..robot.orders import OrderSink


@dataclass
class Ctx:
    s: Settings
    voice: object          # voice.Voice
    llm: LLM
    menu: Menu
    rest: Restaurant
    wps: Waypoints
    robot: Robot
    sink: OrderSink
