#!/usr/bin/env python3
"""
Argo Mini — Wheel Tick Calibrator
Measures the tick-rate ratio between left and right wheels to determine
the correct left_tick_scale for serial_bridge.py.

Usage:
    python3 calibrate_wheels.py [port] [baud] [dac] [seconds]
    python3 calibrate_wheels.py /dev/ttyUSB1 115200 106 5

The robot will drive straight forward for [seconds] seconds at [dac],
measure ticks on both wheels, and report the scale factor to use.

Place the robot on the floor with clear space ahead before running.
"""

import sys, time, serial

PORT    = sys.argv[1] if len(sys.argv) > 1 else '/dev/ttyUSB1'
BAUD    = int(sys.argv[2]) if len(sys.argv) > 2 else 115200
DAC     = int(sys.argv[3]) if len(sys.argv) > 3 else 106
SECONDS = float(sys.argv[4]) if len(sys.argv) > 4 else 5.0

POLE_PAIRS    = 15
TICKS_PER_REV = POLE_PAIRS * 6
WHEEL_RADIUS  = 0.0762
import math
METERS_PER_TICK = (2 * math.pi * WHEEL_RADIUS) / TICKS_PER_REV


def read_odom(ser, timeout=0.2):
    """Read one 'O left right' line from the serial port."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        raw = ser.readline().decode('utf-8', errors='ignore').strip()
        if raw.startswith('O '):
            parts = raw.split()
            if len(parts) == 3:
                return int(parts[1]), int(parts[2])
    return None, None


def main():
    print(f'\nConnecting to {PORT} @ {BAUD} ...')
    try:
        ser = serial.Serial(PORT, BAUD, timeout=0.2)
    except serial.SerialException as e:
        print(f'ERROR: {e}'); sys.exit(1)

    time.sleep(1.5)
    ser.reset_input_buffer()

    print(f'DAC={DAC}  Duration={SECONDS}s')
    print('\nPlace robot on flat floor with clear space ahead.')
    print('Starting in 3 seconds ...')
    for i in range(3, 0, -1):
        print(f'  {i}...', flush=True)
        time.sleep(1.0)

    # Flush stale odom packets
    ser.reset_input_buffer()

    # Read baseline ticks (robot is stopped)
    print('\nReading baseline ...', end='', flush=True)
    l0, r0 = None, None
    for _ in range(5):
        l, r = read_odom(ser, timeout=0.3)
        if l is not None:
            l0, r0 = l, r
            break
    if l0 is None:
        print('\nERROR: no odom data from ESP32. Is it running?')
        ser.close(); sys.exit(1)
    print(f' L={l0}  R={r0}')

    # Drive forward
    print(f'Driving forward at DAC {DAC} for {SECONDS:.1f} s ...', flush=True)
    cmd = f'V {DAC} {DAC}\n'.encode()

    t_start = time.monotonic()
    l_end = r_end = None

    while time.monotonic() - t_start < SECONDS:
        ser.write(cmd)
        ser.flush()
        elapsed = time.monotonic() - t_start
        print(f'\r  {elapsed:.1f}s / {SECONDS:.1f}s', end='', flush=True)
        l, r = read_odom(ser, timeout=0.06)
        if l is not None:
            l_end, r_end = l, r

    # Stop
    ser.write(b'S\n')
    ser.flush()
    print()

    # Final tick reading after stop
    time.sleep(0.2)
    for _ in range(5):
        l, r = read_odom(ser, timeout=0.3)
        if l is not None:
            l_end, r_end = l, r
            break

    if l_end is None:
        print('ERROR: lost serial data during run.')
        ser.close(); sys.exit(1)

    ser.close()

    # ── Results ──────────────────────────────────────────────────────────────
    dl = l_end - l0
    dr = r_end - r0

    print('\n' + '─' * 50)
    print(f'  Left  ticks: {dl:+d}   ({dl * METERS_PER_TICK:.4f} m nominal)')
    print(f'  Right ticks: {dr:+d}   ({dr * METERS_PER_TICK:.4f} m nominal)')

    if dl == 0:
        print('\n  ERROR: left wheel produced 0 ticks — check Hall sensors / wiring.')
        sys.exit(1)
    if dr == 0:
        print('\n  ERROR: right wheel produced 0 ticks — check Hall sensors / wiring.')
        sys.exit(1)

    ratio = dr / dl
    scale = ratio   # left_tick_scale that makes dl_scaled == dr

    print()
    print(f'  Right / Left tick ratio:  {ratio:.4f}')
    print()

    if abs(ratio - 1.0) < 0.05:
        print('  ✓  Wheels are well-matched (ratio within 5%). No scale needed.')
    else:
        print(f'  ✗  Wheels are mismatched ({ratio:.2f}x). Apply left_tick_scale.')
        print()
        print('  ── Recommended fix ─────────────────────────────────────────')
        print(f'  Set left_tick_scale = {scale:.4f} in serial_bridge parameters.')
        print()
        print('  In slam.launch.py and nav.launch.py, update serial_bridge node:')
        print(f"      'left_tick_scale': {scale:.4f},")
        print()
        print('  This makes left-wheel distance match right-wheel distance,')
        print('  fixing straight-line drift and symmetric arc-turn odometry.')

    print('─' * 50)


if __name__ == '__main__':
    main()
