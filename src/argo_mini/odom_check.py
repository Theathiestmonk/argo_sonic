#!/usr/bin/env python3
"""
Argo Mini — Direct Odometry / RPM Checker
==========================================
Reads ESP32 serial output directly — no ROS, no serial_bridge needed.
Run while the ESP32 is powered (motors can be stopped).

Usage:
    python3 odom_check.py [port] [baud]
    python3 odom_check.py /dev/ttyUSB1 115200

ESP32 packet format:
    O <leftTicks> <rightTicks>   — signed cumulative tick counts (20 Hz)
    R <leftRPM>  <rightRPM>     — unsigned RPM magnitudes    (~2 Hz)

Press Ctrl+C to quit.
"""

import sys
import time
import serial

# ── Constants (must match firmware / serial_bridge.py) ─────────────────────
WHEEL_RADIUS    = 0.0762          # m
POLE_PAIRS      = 15
TICKS_PER_REV   = POLE_PAIRS * 6  # 90
METERS_PER_TICK = (2 * 3.14159265 * WHEEL_RADIUS) / TICKS_PER_REV

PORT = sys.argv[1] if len(sys.argv) > 1 else '/dev/ttyUSB1'
BAUD = int(sys.argv[2]) if len(sys.argv) > 2 else 115200


def bar(val, maxval, width=12):
    ratio = max(-1.0, min(1.0, val / maxval)) if maxval else 0.0
    filled = round(abs(ratio) * width)
    empty  = width - filled
    if ratio >= 0:
        return '[' + '·' * empty + '█' * filled + ']'
    else:
        return '[' + '█' * filled + '·' * empty + ']'


def direction(delta):
    if delta > 0:  return '▶ FWD'
    if delta < 0:  return '◀ REV'
    return '  STP'


def main():
    print(f'Connecting to {PORT} @ {BAUD} …')
    try:
        ser = serial.Serial(PORT, BAUD, timeout=0.1)
    except serial.SerialException as e:
        print(f'ERROR: {e}')
        sys.exit(1)

    time.sleep(1.5)
    ser.reset_input_buffer()
    print('Connected.  Waiting for data…\n')

    print('─' * 72)
    print(f'  {"LEFT WHEEL":^32}  {"RIGHT WHEEL":^32}')
    print('─' * 72)

    prev_lt = None
    prev_rt = None
    prev_ts = None

    l_rpm = 0.0
    r_rpm = 0.0

    try:
        while True:
            raw = ser.readline().decode('utf-8', errors='ignore').strip()
            if not raw:
                continue

            now = time.monotonic()

            # ── Tick / odom packet ─────────────────────────────────────────
            if raw.startswith('O '):
                parts = raw.split()
                if len(parts) != 3:
                    continue
                lt = int(parts[1])
                rt = int(parts[2])

                if prev_lt is None:
                    prev_lt, prev_rt, prev_ts = lt, rt, now
                    continue

                dt       = now - prev_ts
                dl_ticks = lt - prev_lt
                dr_ticks = rt - prev_rt
                dl_m     = dl_ticks * METERS_PER_TICK
                dr_m     = dr_ticks * METERS_PER_TICK
                v_l      = dl_m / dt if dt > 0 else 0.0
                v_r      = dr_m / dt if dt > 0 else 0.0

                prev_lt, prev_rt, prev_ts = lt, rt, now

                lbar = bar(dl_ticks, 10)
                rbar = bar(dr_ticks, 10)

                print(
                    f'\r'
                    f'  ticks {lt:+8d}  Δ{dl_ticks:+4d} {direction(dl_ticks)} {lbar}'
                    f'  |  '
                    f'ticks {rt:+8d}  Δ{dr_ticks:+4d} {direction(dr_ticks)} {rbar}'
                    f'  dt={dt*1000:.0f}ms',
                    end='', flush=True
                )

            # ── RPM packet ─────────────────────────────────────────────────
            elif raw.startswith('R '):
                parts = raw.split()
                if len(parts) != 3:
                    continue
                l_rpm = float(parts[1])
                r_rpm = float(parts[2])
                # Print RPM on its own line so it doesn't overwrite odom line
                print(
                    f'\n  RPM  L={l_rpm:6.1f}  R={r_rpm:6.1f}'
                    f'   speed_L={l_rpm/60*TICKS_PER_REV*METERS_PER_TICK:.3f} m/s'
                    f'   speed_R={r_rpm/60*TICKS_PER_REV*METERS_PER_TICK:.3f} m/s',
                    flush=True
                )

    except KeyboardInterrupt:
        print('\n\nQuit.')
    finally:
        ser.close()


if __name__ == '__main__':
    main()
