#!/usr/bin/env python3
"""
argo_teleop.py  -  Keyboard teleoperation for Argo Mini (ESP32)
================================================================
Serial protocol (matches ESP32 firmware exactly):
  Send:    "V <left_dac> <right_dac>\n"   unsigned positive ints only
           "S\n"                           emergency stop
  Receive: "O <leftTicks> <rightTicks>\n" odometry at 20 Hz (ignored here)

Controls
--------
  W       - increase forward speed
  S       - decrease forward speed (floors at 0, never reverses)
  A / D   - hold to arc-turn left / right; release to straighten
  SPACE   - emergency stop (zero everything)
  Q       - quit

Constraints enforced (hard, no exceptions)
------------------------------------------
  * Forward-only  : wheel DAC is always >= 0; reverse is impossible
  * No tank turns : angular is scaled so the inner wheel never goes below 0
  * DAC output    : always 0 (coast) or clamped to [DAC_MIN=104, DAC_MAX=120]
"""

import sys
import tty
import termios
import select
import serial
import time

# ?? Serial ????????????????????????????????????????????????????????????????????
PORT = '/dev/ttyUSB1'
BAUD = 115200

# ?? Geometry ??????????????????????????????????????????????????????????????????
WHEEL_BASE = 0.41          # m  (matches serial_bridge.py)

# ?? Speed steps ???????????????????????????????????????????????????????????????
LIN_STEP = 0.02           # m/s added per W press
LIN_MAX  = 0.10            # m/s hard ceiling

# Angular velocity while A / D is held (released ? 0 immediately)
ANG_HOLD = 0.3             # rad/s

# ?? DAC constants (must mirror firmware constrain() call) ?????????????????????
DAC_MIN  = 105
DAC_MAX  = 108

# ?? Core conversion ???????????????????????????????????????????????????????????

def _clamp(val, lo, hi):
    return max(lo, min(hi, val))


def vel_to_dac(wheel_ms: float) -> int:
    """
    Forward-only: any negative value ? 0 (coast).
    Positive     : mapped linearly from LIN_MAX ? [DAC_MIN, DAC_MAX].
    """
    if wheel_ms <= 0.0:          # covers negative AND true zero
        return 0
    mag = int(round(DAC_MIN + (wheel_ms / LIN_MAX) * (DAC_MAX - DAC_MIN)))
    return _clamp(mag, DAC_MIN, DAC_MAX)


def diff_drive(lin: float, ang: float):
    """
    Convert (linear m/s, angular rad/s) ? (dac_left, dac_right).

    Two guarantees:
      1. lin is clamped to [0, LIN_MAX] before anything else.
      2. ang is reduced so that min(v_l, v_r) >= 0 ? no wheel ever reverses.
    """
    lin = _clamp(lin, 0.0, LIN_MAX)

    # Raw wheel velocities
    half = WHEEL_BASE / 2.0
    v_l  = lin - ang * half
    v_r  = lin + ang * half

    # Scale ang down if either wheel would go negative
    if v_l < 0.0 or v_r < 0.0:
        # Largest |ang| that keeps both wheels at exactly 0 or above:
        #   lin - |ang|*half >= 0  =>  |ang| <= lin/half
        max_ang = lin / half if lin > 1e-6 else 0.0
        ang     = _clamp(ang, -max_ang, max_ang)
        v_l     = lin - ang * half
        v_r     = lin + ang * half

    # Final safety clamp ? belt and suspenders
    v_l = max(v_l, 0.0)
    v_r = max(v_r, 0.0)

    # Proportional scale if peak exceeds LIN_MAX
    peak = max(v_l, v_r)
    if peak > LIN_MAX:
        scale = LIN_MAX / peak
        v_l  *= scale
        v_r  *= scale

    return vel_to_dac(v_l), vel_to_dac(v_r)


# ?? Non-blocking keyboard ?????????????????????????????????????????????????????

def get_key(timeout: float = 0.05) -> str:
    """Return one character if available within timeout, else ''."""
    fd  = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        ready, _, _ = select.select([sys.stdin], [], [], timeout)
        return sys.stdin.read(1) if ready else ''
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


# ?? Main ??????????????????????????????????????????????????????????????????????

def main():
    print(f"Opening {PORT} @ {BAUD} ?")
    try:
        ser = serial.Serial(PORT, BAUD, timeout=0.05)
    except serial.SerialException as e:
        print(f"ERROR: {e}")
        sys.exit(1)

    time.sleep(2.0)
    ser.reset_input_buffer()

    print(
        "Ready.\n"
        "  W      ? speed up (forward only)\n"
        "  S      ? slow down (stops at 0, no reverse)\n"
        "  A / D  ? arc-turn left / right while held\n"
        "  SPACE  ? emergency stop\n"
        "  Q      ? quit\n"
    )

    lin_vel = 0.0
    ang_vel = 0.0            # set while key held, cleared on release

    def send(l: int, r: int):
        ser.write(f"V {l} {r}\n".encode())
        ser.flush()

    def estop():
        ser.write(b"S\n")
        ser.flush()

    try:
        while True:
            # Keep serial rx buffer clear
            while ser.in_waiting:
                ser.readline()

            key = get_key()

            if key in ('w', 'W'):
                lin_vel = _clamp(lin_vel + LIN_STEP, 0.0, LIN_MAX)
                ang_vel = 0.0
            elif key in ('s', 'S'):
                lin_vel = _clamp(lin_vel - LIN_STEP, 0.0, LIN_MAX)
                ang_vel = 0.0
            elif key in ('a', 'A'):
                ang_vel = ANG_HOLD          # held left turn
            elif key in ('d', 'D'):
                ang_vel = -ANG_HOLD         # held right turn
            elif key == ' ':
                lin_vel = 0.0
                ang_vel = 0.0
                estop()
                print("\rEMERGENCY STOP                              ", flush=True)
                continue
            elif key in ('q', 'Q', '\x03'):
                break
            else:
                # No key (timeout) or unrecognised ? stop turning
                ang_vel = 0.0

            l_dac, r_dac = diff_drive(lin_vel, ang_vel)
            send(l_dac, r_dac)

            print(
                f"\r  lin={lin_vel:+.2f} m/s  "
                f"ang={ang_vel:+.2f} rad/s  "
                f"DAC L={l_dac:3d}  R={r_dac:3d}   ",
                end='', flush=True
            )

    except KeyboardInterrupt:
        pass
    finally:
        print("\nStopping ?")
        try:
            estop()
            time.sleep(0.1)
            ser.close()
        except Exception:
            pass
        print("Done.")


if __name__ == '__main__':
    main()