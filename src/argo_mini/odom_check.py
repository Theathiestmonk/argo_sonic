#!/usr/bin/env python3
"""
Argo Mini — Direct Odom Check + Drive
======================================
Reads ESP32 serial directly (no ROS) AND drives the robot via keyboard.

Usage:
    python3 odom_check.py [port] [baud]
    python3 odom_check.py /dev/ttyUSB1 115200

Controls:
    W / ↑   forward        S / ↓   reverse
    A / ←   arc-left       D / →   arc-right
    SPACE   stop           Ctrl+C  quit (sends stop first)

Arc-turn only: inner wheel never reverses — same rule as the full stack.
No ROS or serial_bridge needed. Run with just the ESP32 powered.

WARNING: depth_safety_shield is NOT active here. Watch the robot.
"""

import math
import sys
import termios
import threading
import time
import tty

import serial

# ── Hardware constants (must match serial_bridge.py / firmware) ────────────
WHEEL_RADIUS    = 0.0762
WHEEL_BASE      = 0.40
POLE_PAIRS      = 15
TICKS_PER_REV   = POLE_PAIRS * 6          # 90
METERS_PER_TICK = (2 * math.pi * WHEEL_RADIUS) / TICKS_PER_REV

DAC_STOP = 0
DAC_MIN  = 104
DAC_MAX  = 108
VMAX     = 0.40
V_DEAD   = 0.04

# ── Teleop constants ────────────────────────────────────────────────────────
LIN_MAX     = 0.20    # m/s
ANG_MAX     = 1.2     # rad/s
LIN_ACCEL   = 0.04    # m/s per 50 ms tick
ANG_ACCEL   = 0.20    # rad/s per 50 ms tick
LIN_DECAY   = 0.78
ANG_DECAY   = 0.68
KEY_TIMEOUT = 0.12    # s — release threshold
NUDGE       = 0.05    # m/s forward added when turning with lin ≈ 0

PORT = sys.argv[1] if len(sys.argv) > 1 else '/dev/ttyUSB1'
BAUD = int(sys.argv[2]) if len(sys.argv) > 2 else 115200


# ── Shared state ────────────────────────────────────────────────────────────
state = {
    'lt': 0, 'rt': 0,
    'dl': 0, 'dr': 0,
    'l_rpm': 0.0, 'r_rpm': 0.0,
    'dt_ms': 0.0,
    'lin': 0.0, 'ang': 0.0,
    'last_key': 0.0,
}
state_lock = threading.Lock()
quit_flag  = threading.Event()


# ── Helpers ─────────────────────────────────────────────────────────────────

def v_to_dac(v):
    if abs(v) < V_DEAD:
        return DAC_STOP
    ratio = min(1.0, (abs(v) - V_DEAD) / (VMAX - V_DEAD))
    dac   = round(DAC_MIN + ratio * (DAC_MAX - DAC_MIN))
    return dac if v > 0 else -dac


def arc_clamp(lin, ang):
    """Prevent inner wheel from reversing — arc turns only."""
    if abs(ang) < 0.01:
        return lin, ang
    if abs(lin) < 0.01:
        lin = NUDGE
    max_ang = abs(lin) / (WHEEL_BASE / 2.0)
    ang = math.copysign(min(abs(ang), max_ang), ang)
    return lin, ang


def wheel_dacs(lin, ang):
    lin, ang = arc_clamp(lin, ang)
    v_l = lin - ang * (WHEEL_BASE / 2.0)
    v_r = lin + ang * (WHEEL_BASE / 2.0)
    peak = max(abs(v_l), abs(v_r))
    if peak > VMAX:
        v_l = v_l / peak * VMAX
        v_r = v_r / peak * VMAX
    dac_l = v_to_dac(v_l)
    dac_r = v_to_dac(v_r)
    # Belt-and-suspenders: enforce no opposite-sign wheels
    if dac_l > 0 and dac_r < 0:
        dac_r = DAC_STOP
    elif dac_l < 0 and dac_r > 0:
        dac_l = DAC_STOP
    return dac_l, dac_r


def bar(val, maxval, width=10):
    ratio  = max(-1.0, min(1.0, val / maxval)) if maxval else 0.0
    filled = round(abs(ratio) * width)
    empty  = width - filled
    if ratio >= 0:
        return '[' + '·' * empty + '█' * filled + ']'
    return '[' + '█' * filled + '·' * empty + ']'


def dirstr(delta):
    if delta > 0: return '\033[32m▶ FWD\033[0m'
    if delta < 0: return '\033[31m◀ REV\033[0m'
    return '  STP'


# ── Serial reader thread ────────────────────────────────────────────────────

def serial_reader(ser):
    prev_lt = prev_rt = prev_ts = None
    while not quit_flag.is_set():
        try:
            raw = ser.readline().decode('utf-8', errors='ignore').strip()
        except Exception:
            break
        if not raw:
            continue
        now = time.monotonic()

        if raw.startswith('O '):
            parts = raw.split()
            if len(parts) != 3:
                continue
            lt, rt = int(parts[1]), int(parts[2])
            if prev_lt is None:
                prev_lt, prev_rt, prev_ts = lt, rt, now
                continue
            dt = now - prev_ts
            with state_lock:
                state['dl']    = lt - prev_lt
                state['dr']    = rt - prev_rt
                state['lt']    = lt
                state['rt']    = rt
                state['dt_ms'] = dt * 1000
            prev_lt, prev_rt, prev_ts = lt, rt, now

        elif raw.startswith('R '):
            parts = raw.split()
            if len(parts) == 3:
                with state_lock:
                    state['l_rpm'] = float(parts[1])
                    state['r_rpm'] = float(parts[2])


# ── Keyboard thread ─────────────────────────────────────────────────────────

def keyboard_reader():
    fd  = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    tty.setraw(fd)
    try:
        while not quit_flag.is_set():
            ch = sys.stdin.read(1)
            if ch == '\x1b':
                try:
                    ch += sys.stdin.read(2)
                except Exception:
                    pass
            if ch in ('\x03', '\x04'):
                quit_flag.set()
                break
            with state_lock:
                state['last_key'] = time.monotonic()
                if ch in ('w', '\x1b[A'):
                    state['lin'] = min(state['lin'] + LIN_ACCEL,  LIN_MAX)
                elif ch in ('s', '\x1b[B'):
                    state['lin'] = max(state['lin'] - LIN_ACCEL, -LIN_MAX)
                elif ch in ('a', '\x1b[D'):
                    state['ang'] = min(state['ang'] + ANG_ACCEL,  ANG_MAX)
                elif ch in ('d', '\x1b[C'):
                    state['ang'] = max(state['ang'] - ANG_ACCEL, -ANG_MAX)
                elif ch == ' ':
                    state['lin'] = 0.0
                    state['ang'] = 0.0
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


# ── Main loop ───────────────────────────────────────────────────────────────

def main():
    print(f'Connecting to {PORT} @ {BAUD} …')
    try:
        ser = serial.Serial(PORT, BAUD, timeout=0.1)
    except serial.SerialException as e:
        print(f'ERROR: {e}')
        sys.exit(1)

    time.sleep(1.5)
    ser.reset_input_buffer()

    # Start threads
    reader_t  = threading.Thread(target=serial_reader,  args=(ser,), daemon=True)
    keyboard_t = threading.Thread(target=keyboard_reader, daemon=True)
    reader_t.start()
    keyboard_t.start()

    # Banner
    print('\033[2J\033[H', end='', flush=True)
    print('┌──────────────────────────────────────────────────────────────────┐')
    print('│        ARGO MINI  —  Direct Drive + Odom Check                   │')
    print('│  W/↑ fwd  S/↓ rev  A/← left  D/→ right  SPACE stop  Ctrl+C quit │')
    print('│  Arc-turn only. No ROS. WATCH THE ROBOT — no safety shield active │')
    print('└──────────────────────────────────────────────────────────────────┘')
    print()

    TICK_MS = 50   # 20 Hz

    try:
        while not quit_flag.is_set():
            t0 = time.monotonic()

            # Decay and read state
            with state_lock:
                age = t0 - state['last_key']
                if age > KEY_TIMEOUT:
                    state['lin'] *= LIN_DECAY
                    state['ang'] *= ANG_DECAY
                    if abs(state['lin']) < 0.005: state['lin'] = 0.0
                    if abs(state['ang']) < 0.01:  state['ang'] = 0.0
                lin  = state['lin']
                ang  = state['ang']
                lt   = state['lt']
                rt   = state['rt']
                dl   = state['dl']
                dr   = state['dr']
                lrpm = state['l_rpm']
                rrpm = state['r_rpm']
                dtms = state['dt_ms']

            # Send motor command
            dac_l, dac_r = wheel_dacs(lin, ang)
            try:
                ser.write(f'V {dac_l} {dac_r}\n'.encode())
                ser.flush()
            except Exception:
                pass

            # Display (3 lines, overwrite in place)
            l_speed = lrpm / 60.0 * TICKS_PER_REV * METERS_PER_TICK
            r_speed = rrpm / 60.0 * TICKS_PER_REV * METERS_PER_TICK

            print(
                f'\033[3A'   # move cursor up 3 lines
                f'\r  ODOM  L ticks={lt:+9d}  Δ={dl:+4d} {dirstr(dl)} {bar(dl,10)}   '
                f'R ticks={rt:+9d}  Δ={dr:+4d} {dirstr(dr)} {bar(dr,10)}  dt={dtms:.0f}ms\n'
                f'\r  RPM   L={lrpm:6.1f} ({l_speed:.3f}m/s)                              '
                f'R={rrpm:6.1f} ({r_speed:.3f}m/s)\n'
                f'\r  CMD   lin={lin:+.3f}m/s {bar(lin,LIN_MAX)}  '
                f'ang={ang:+.3f}r/s {bar(ang,ANG_MAX)}  '
                f'DAC L={dac_l:+4d} R={dac_r:+4d}\n',
                end='', flush=True
            )

            # Sleep for remainder of tick
            elapsed = (time.monotonic() - t0) * 1000
            sleep_ms = max(0, TICK_MS - elapsed)
            time.sleep(sleep_ms / 1000.0)

    except KeyboardInterrupt:
        pass
    finally:
        try:
            ser.write(b'S\n')
            ser.flush()
        except Exception:
            pass
        ser.close()
        print('\n\nStopped. Port closed.')


if __name__ == '__main__':
    main()
