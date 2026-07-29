#!/usr/bin/env python3
"""
Argo Mini SLAM teleop — forward, reverse, pivot, arrow keys, adjustable speed.

  w / Up       forward
  x / Down     reverse
  a / Left     pivot left  (left stops, right runs)
  d / Right    pivot right (right stops, left runs)
  s / space    stop
  + / -        speed up / speed down

  Ctrl+C   quit
"""

import sys
import select
import time
import tty
import termios
import rclpy
from rclpy.node import Node
from geometry_msgs.msg import Twist

BASE_LIN_VEL = 0.15   # m/s  — keep slow for good SLAM at 1.0x speed scale
BASE_ANG_VEL = 0.50   # rad/s

# Multiplier on BASE_LIN_VEL/BASE_ANG_VEL, adjustable at runtime with +/-.
# Not hard-clamped tightly here — serial_bridge.py itself caps to VMAX
# (0.40 m/s) regardless, scaling both wheels down proportionally if
# exceeded, so this range just gives a sane "Slow ... Fast" feel without
# needing to duplicate that cap.
SPEED_MIN, SPEED_MAX, SPEED_STEP = 0.3, 2.5, 0.1

# serial_bridge.py ramps commanded RPM toward the target by at most
# _RPM_RAMP (5.0) per /cmd_vel message received, not per second — a single
# stop command only steps speed down a little, not to a full stop (same
# bug independently confirmed and fixed today in TeleopPad.jsx and
# backend/launcher.py's /estop). STOP_REPEAT_COUNT zero commands at
# STOP_REPEAT_S apart comfortably covers the worst-case ramp-down
# regardless of current speed.
STOP_REPEAT_COUNT = 12
STOP_REPEAT_S     = 0.08

# Each action is a direction (lin/ang sign+shape); actual speed is applied
# at publish time using the current speed_scale, not baked in here.
ACTIONS = {
    'FORWARD':     ( 1.0,  0.0),
    'REVERSE':     (-1.0,  0.0),
    'PIVOT_LEFT':  ( 0.0,  1.0),
    'PIVOT_RIGHT': ( 0.0, -1.0),
    'STOP':        ( 0.0,  0.0),
}

# Plain keys and arrow-key escape sequences both resolve to the same
# action names — neither layout is the "real" one, they're just two ways
# to trigger the same four directions plus stop.
KEY_TO_ACTION = {
    'w': 'FORWARD', 'x': 'REVERSE', 'a': 'PIVOT_LEFT', 'd': 'PIVOT_RIGHT',
    's': 'STOP', ' ': 'STOP',
}
# Arrow keys arrive as the 3-byte sequence ESC [ <letter> — 'A'/'B'/'C'/'D'
# for up/down/right/left respectively, the standard ANSI cursor-key codes.
ARROW_TO_ACTION = {
    'A': 'FORWARD', 'B': 'REVERSE', 'C': 'PIVOT_RIGHT', 'D': 'PIVOT_LEFT',
}

LABELS = {
    'FORWARD': 'FORWARD', 'REVERSE': 'REVERSE',
    'PIVOT_LEFT': 'PIVOT LEFT', 'PIVOT_RIGHT': 'PIVOT RIGHT', 'STOP': 'STOP',
}

BANNER = """
=========================================
  ARGO MINI — SLAM TELEOP
=========================================
  w / Up      → forward
  x / Down    → reverse
  a / Left    → pivot left
  d / Right   → pivot right
  s / space   → stop
  + / -       → speed up / down
  Ctrl+C      → quit
=========================================
"""


def get_key(settings):
    """Read one key. Arrow keys are ESC ('\\x1b') followed by '[' and a
    direction letter — after seeing ESC, a short select() checks whether
    more bytes are already waiting (a real arrow sequence arrives as one
    fast burst from the terminal) rather than blocking indefinitely, so a
    bare Escape press on its own doesn't hang waiting for bytes that will
    never come. Returns a single character for plain keys, or 'CURSOR_X'
    (X = A/B/C/D) for a recognized arrow key."""
    tty.setraw(sys.stdin.fileno())
    try:
        ch = sys.stdin.read(1)
        if ch == '\x1b':
            if select.select([sys.stdin], [], [], 0.05)[0]:
                ch2 = sys.stdin.read(1)
                if ch2 == '[' and select.select([sys.stdin], [], [], 0.05)[0]:
                    ch3 = sys.stdin.read(1)
                    if ch3 in ARROW_TO_ACTION:
                        return f'CURSOR_{ch3}'
            return '\x1b'
        return ch
    finally:
        termios.tcsetattr(sys.stdin, termios.TCSADRAIN, settings)


def publish_stop_until_zero(pub):
    """Repeat the zero-velocity command instead of sending it once — see
    STOP_REPEAT_COUNT's comment above for why a single publish isn't
    enough to actually bring the robot to a stop at real speed."""
    stop = Twist()
    for _ in range(STOP_REPEAT_COUNT):
        pub.publish(stop)
        time.sleep(STOP_REPEAT_S)


def main():
    rclpy.init()
    node = Node('slam_teleop')
    pub  = node.create_publisher(Twist, '/cmd_vel', 10)

    settings = termios.tcgetattr(sys.stdin)
    print(BANNER)

    speed_scale = 1.0

    try:
        while rclpy.ok():
            key = get_key(settings)
            if key == '\x03':   # Ctrl+C
                break

            if key in ('+', '='):
                speed_scale = min(SPEED_MAX, speed_scale + SPEED_STEP)
                print(f'\r  SPEED  {speed_scale:.1f}x  '
                      f'(lin={BASE_LIN_VEL*speed_scale:.2f} m/s  ang={BASE_ANG_VEL*speed_scale:.2f} rad/s)   ',
                      end='', flush=True)
                continue
            if key == '-':
                speed_scale = max(SPEED_MIN, speed_scale - SPEED_STEP)
                print(f'\r  SPEED  {speed_scale:.1f}x  '
                      f'(lin={BASE_LIN_VEL*speed_scale:.2f} m/s  ang={BASE_ANG_VEL*speed_scale:.2f} rad/s)   ',
                      end='', flush=True)
                continue

            if key.startswith('CURSOR_'):
                action = ARROW_TO_ACTION[key[-1]]
            else:
                action = KEY_TO_ACTION.get(key)
            if action is None:
                continue

            lin_dir, ang_dir = ACTIONS[action]
            lin = lin_dir * BASE_LIN_VEL * speed_scale
            ang = ang_dir * BASE_ANG_VEL * speed_scale
            label = LABELS[action]

            if action == 'STOP':
                print(f'\r  {label:<14}  lin={lin:+.2f}  ang={ang:+.2f}   ', end='', flush=True)
                publish_stop_until_zero(pub)
                continue

            msg = Twist()
            msg.linear.x  = lin
            msg.angular.z = ang
            pub.publish(msg)
            print(f'\r  {label:<14}  lin={lin:+.2f}  ang={ang:+.2f}   ', end='', flush=True)

    finally:
        # Send stop on exit — repeated, not a single publish, so quitting
        # (Ctrl+C) doesn't leave the robot still moving at whatever speed
        # it had (see STOP_REPEAT_COUNT's comment above).
        publish_stop_until_zero(pub)
        termios.tcsetattr(sys.stdin, termios.TCSADRAIN, settings)
        print('\n[teleop] stopped.')
        node.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    main()
