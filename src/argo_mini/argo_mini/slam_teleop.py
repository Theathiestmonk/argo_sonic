#!/usr/bin/env python3
"""
SLAM Teleop — Argo Mini

Designed for clean map-making:
  • Velocity is held while a key is pressed and decays smoothly on release
    (no need to repeatedly tap keys)
  • Arc turns only: when a turn would reverse the inner wheel the angular
    velocity is clamped and a small forward nudge is applied so the robot
    always arcs rather than pivots — keeps odometry clean during SLAM

Controls:
  W / ↑   accelerate forward     S / ↓   accelerate reverse
  A / ←   turn left              D / →   turn right
  SPACE   immediate brake        Ctrl+C  quit

Publishes to /cmd_vel at 20 Hz.
Run in a separate terminal alongside slam.launch.py.
"""

import math
import sys
import termios
import threading
import time
import tty

import rclpy
from rclpy.node import Node
from geometry_msgs.msg import Twist


# ── Constants ─────────────────────────────────────────────────────────────────
WHEEL_BASE  = 0.40    # m — must match serial_bridge.py

LIN_MAX     = 0.25    # m/s
ANG_MAX     = 1.2     # rad/s
LIN_ACCEL   = 0.10    # m/s per 50 ms tick — first press gives 0.10 m/s → DAC 105
ANG_ACCEL   = 0.25    # rad/s per 50 ms tick
LIN_DECAY   = 0.78    # multiplier per 50 ms tick when no key held (~0.5 s to stop)
ANG_DECAY   = 0.68
KEY_TIMEOUT = 0.12    # s  — treat key as released if nothing received in this window
NUDGE       = 0.12    # m/s forward nudge when turning with lin ≈ 0 (gives DAC 105)


class SlamTeleop(Node):

    def __init__(self):
        super().__init__('slam_teleop')
        self._pub = self.create_publisher(Twist, '/cmd_vel', 10)

        self._lin      = 0.0
        self._ang      = 0.0
        self._last_key = time.monotonic()
        self._lock     = threading.Lock()
        self._quit     = False

        self.create_timer(0.05, self._tick)   # 20 Hz

        t = threading.Thread(target=self._read_keys, daemon=True)
        t.start()

        self._print_banner()

    # ── keyboard thread ───────────────────────────────────────────────────────

    def _read_keys(self):
        fd  = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        tty.setraw(fd)
        try:
            while not self._quit:
                ch = sys.stdin.read(1)
                if ch == '\x1b':
                    try:
                        ch += sys.stdin.read(2)   # arrow escape sequence
                    except Exception:
                        pass
                if ch in ('\x03', '\x04'):         # Ctrl+C / Ctrl+D
                    self._quit = True
                    rclpy.shutdown()
                    break
                self._handle_key(ch)
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
            # Restore cursor and print newline so shell prompt is clean
            print('\r')

    def _handle_key(self, ch: str):
        with self._lock:
            self._last_key = time.monotonic()
            if ch in ('w', '\x1b[A'):           # forward
                self._lin = min(self._lin + LIN_ACCEL,  LIN_MAX)
            elif ch in ('s', '\x1b[B'):          # reverse
                self._lin = max(self._lin - LIN_ACCEL, -LIN_MAX)
            elif ch in ('a', '\x1b[D'):          # left
                self._ang = min(self._ang + ANG_ACCEL,  ANG_MAX)
            elif ch in ('d', '\x1b[C'):          # right
                self._ang = max(self._ang - ANG_ACCEL, -ANG_MAX)
            elif ch == ' ':                      # brake
                self._lin = 0.0
                self._ang = 0.0

    # ── publish / decay timer ─────────────────────────────────────────────────

    def _tick(self):
        with self._lock:
            age = time.monotonic() - self._last_key
            if age > KEY_TIMEOUT:
                # No key received recently — decay toward zero
                self._lin *= LIN_DECAY
                self._ang *= ANG_DECAY
                if abs(self._lin) < 0.005:
                    self._lin = 0.0
                if abs(self._ang) < 0.01:
                    self._ang = 0.0

            lin, ang = self._arc_clamp(self._lin, self._ang)

        msg = Twist()
        msg.linear.x   = lin
        msg.angular.z  = ang
        self._pub.publish(msg)
        self._render(lin, ang)

    # ── arc-turn constraint ───────────────────────────────────────────────────

    def _arc_clamp(self, lin: float, ang: float):
        """
        Ensure neither wheel is commanded in the opposite direction to the other.

        For a differential drive:
            v_left  = lin - ang * (WHEEL_BASE / 2)
            v_right = lin + ang * (WHEEL_BASE / 2)

        No wheel reversal ↔ |ang| <= |lin| / (WHEEL_BASE / 2).

        When lin ≈ 0 and ang is requested, a small forward nudge is added
        so the robot arcs around the stationary inner wheel instead of pivoting.
        """
        if abs(ang) < 0.01:
            return lin, ang

        if abs(lin) < 0.01:
            # Pure rotation requested — nudge forward so it becomes an arc
            lin = NUDGE

        max_ang = abs(lin) / (WHEEL_BASE / 2.0)
        ang = math.copysign(min(abs(ang), max_ang), ang)
        return lin, ang

    # ── display ───────────────────────────────────────────────────────────────

    def _print_banner(self):
        print('\033[2J\033[H', end='', flush=True)   # clear screen, cursor home
        print('┌──────────────────────────────────────────────────┐')
        print('│          ARGO MINI  —  SLAM TELEOP               │')
        print('├──────────────────────────────────────────────────┤')
        print('│  W/↑  forward      S/↓  reverse                  │')
        print('│  A/←  turn left    D/→  turn right               │')
        print('│  SPACE  brake      Ctrl+C  quit                   │')
        print('│  Arc turns only — inner wheel never reverses      │')
        print('└──────────────────────────────────────────────────┘')
        print()

    def _render(self, lin: float, ang: float):
        lin_bar = self._bar(lin, LIN_MAX, 8)
        ang_bar = self._bar(ang, ANG_MAX, 8)
        print(
            f'\r  Lin {lin_bar} {lin:+5.2f} m/s'
            f'   Ang {ang_bar} {ang:+5.2f} r/s   ',
            end='', flush=True,
        )

    @staticmethod
    def _bar(val: float, top: float, width: int) -> str:
        ratio  = max(-1.0, min(1.0, val / top)) if top else 0.0
        filled = round(abs(ratio) * width)
        empty  = width - filled
        if ratio >= 0:
            return '[' + '·' * empty + '█' * filled + ']'
        else:
            return '[' + '█' * filled + '·' * empty + ']'


def main(args=None):
    rclpy.init(args=args)
    node = SlamTeleop()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        # Send a stop command before exiting
        try:
            node._pub.publish(Twist())
        except Exception:
            pass
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == '__main__':
    main()
