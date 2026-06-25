#!/usr/bin/env python3
"""
Simple SLAM Teleop ? Argo Mini
Run:  python3 teleop.py

Controls
  W / ?   forward       S / ?   reverse
  A / ?   turn left     D / ?   turn right
  SPACE   stop          Ctrl+C  quit

No tank turns: pressing A/D alone adds a forward nudge so the
robot arcs instead of spinning in place.
"""

import sys
import termios
import threading
import time
import tty

import rclpy
from rclpy.node import Node
from geometry_msgs.msg import Twist

# ?? Tune these ????????????????????????????????????????????????????????????????
LINEAR_SPEED  = 0.15   # m/s  ? keep slow for clean SLAM scans
ANGULAR_SPEED = 0.60   # rad/s
ARC_NUDGE     = 0.10   # m/s forward nudge added when turning with lin ? 0
PUBLISH_HZ    = 20
# ?????????????????????????????????????????????????????????????????????????????


def get_key(timeout: float = 0.1) -> str:
    """Read one keypress from stdin (raw mode, non-blocking with timeout)."""
    fd  = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        ch = sys.stdin.read(1)
        if ch == '\x1b':
            # Arrow key: ESC [ A/B/C/D
            try:
                ch += sys.stdin.read(2)
            except Exception:
                pass
        return ch
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


class Teleop(Node):

    def __init__(self):
        super().__init__('slam_teleop_simple')
        self._pub  = self.create_publisher(Twist, '/cmd_vel', 10)
        self._lin  = 0.0
        self._ang  = 0.0
        self._lock = threading.Lock()
        self._quit = False

        # Publish at fixed rate in background
        threading.Thread(target=self._publish_loop, daemon=True).start()
        # Read keys in background
        threading.Thread(target=self._key_loop,     daemon=True).start()

        self._print_banner()

    # ?? key reading ???????????????????????????????????????????????????????????

    def _key_loop(self):
        fd  = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        tty.setraw(fd)
        try:
            while not self._quit:
                ch = sys.stdin.read(1)
                if ch == '\x1b':
                    try:
                        ch += sys.stdin.read(2)
                    except Exception:
                        pass
                self._handle(ch)
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
            print('\r')

    def _handle(self, ch: str):
        with self._lock:
            if ch in ('\x03', '\x04', 'q'):     # Ctrl+C / Ctrl+D / q
                self._quit = True
                self._lin  = 0.0
                self._ang  = 0.0
                return

            if ch in ('w', '\x1b[A'):            # forward
                self._lin =  LINEAR_SPEED
                self._ang =  0.0
            elif ch in ('s', '\x1b[B'):           # reverse
                self._lin = -LINEAR_SPEED
                self._ang =  0.0
            elif ch in ('a', '\x1b[D'):           # arc left
                self._ang =  ANGULAR_SPEED
                if abs(self._lin) < 0.01:
                    self._lin = ARC_NUDGE         # nudge forward ? arc, not spin
            elif ch in ('d', '\x1b[C'):           # arc right
                self._ang = -ANGULAR_SPEED
                if abs(self._lin) < 0.01:
                    self._lin = ARC_NUDGE
            elif ch == ' ':                       # stop
                self._lin =  0.0
                self._ang =  0.0

    # ?? publish loop ??????????????????????????????????????????????????????????

    def _publish_loop(self):
        dt = 1.0 / PUBLISH_HZ
        while not self._quit:
            with self._lock:
                lin = self._lin
                ang = self._ang
            msg = Twist()
            msg.linear.x  = lin
            msg.angular.z = ang
            self._pub.publish(msg)
            self._render(lin, ang)
            time.sleep(dt)
        # send zero on exit
        self._pub.publish(Twist())

    # ?? display ???????????????????????????????????????????????????????????????

    def _print_banner(self):
        print('\033[2J\033[H', end='', flush=True)
        print('????????????????????????????????????????????')
        print('?     ARGO MINI  ?  SLAM TELEOP            ?')
        print('????????????????????????????????????????????')
        print('?  W / ?   forward      S / ?   reverse   ?')
        print('?  A / ?   arc left     D / ?   arc right  ?')
        print('?  SPACE   stop         Q / Ctrl+C  quit   ?')
        print('????????????????????????????????????????????')
        print('?  No tank turns ? A/D add forward nudge   ?')
        print('????????????????????????????????????????????')
        print()

    def _render(self, lin: float, ang: float):
        direction = (
            'FORWARD' if lin > 0.01 else
            'REVERSE' if lin < -0.01 else
            'STOPPED'
        )
        turn = (
            'LEFT ' if ang > 0.01 else
            'RIGHT' if ang < -0.01 else
            '     '
        )
        print(
            f'\r  {direction}  turn={turn}  '
            f'lin={lin:+.2f} m/s  ang={ang:+.2f} r/s   ',
            end='', flush=True,
        )


def main():
    rclpy.init()
    node = Teleop()
    try:
        while rclpy.ok() and not node._quit:
            rclpy.spin_once(node, timeout_sec=0.05)
    except KeyboardInterrupt:
        pass
    finally:
        node._pub.publish(Twist())
        node.destroy_node()
        rclpy.shutdown()
        print('\nBye.')


if __name__ == '__main__':
    main()
