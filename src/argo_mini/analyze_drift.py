#!/usr/bin/env python3
"""
Argo Mini — Straight-Line Drift Analyzer

Measures whether commanding equal left/right wheel velocity actually drives
the robot straight, and computes the LEFT_TICK_SCALE correction needed if not.

Why this is a different tool from calibrate_wheels.py:
calibrate_wheels.py measures the raw left/right TICK ratio while the firmware's
own PI velocity controller is actively converging both wheels toward the same
target RPM. That loop makes measRpmL (= raw_ticks_L * K * LEFT_TICK_SCALE) and
measRpmR (= raw_ticks_R * K) both settle to the commanded target — which means
the measured tick ratio just re-confirms whatever LEFT_TICK_SCALE is ALREADY
baked into the firmware, regardless of whether that value is actually correct.
If you already have a non-1.0 scale applied and the robot still drifts, running
that tool again will circularly report "your current scale is about right."

This tool breaks that circularity with an independent ground-truth measurement:
how far the robot actually drifts sideways over a known forward distance. That
physical measurement doesn't depend on any encoder-scale assumption at all.

Usage:
    python3 analyze_drift.py [port] [baud] [target_rpm] [seconds] [current_scale]
    python3 analyze_drift.py /dev/ttyUSB0 115200 40 5 0.66

Procedure:
    1. Mark the robot's exact starting position and heading (e.g. tape line on
       the floor along its forward direction).
    2. Run this script — the robot drives straight forward for [seconds].
    3. When it stops, measure the perpendicular distance from the tape line to
       the robot's actual centre (positive = drifted right, negative = left),
       in centimetres, and enter it when prompted.
    4. The script reports the corrected LEFT_TICK_SCALE to put in
       esp32_motor_controller.ino, then re-flash and re-run to verify (drift
       should now measure close to 0 cm).
"""

import sys, time, math, serial

PORT          = sys.argv[1] if len(sys.argv) > 1 else '/dev/ttyUSB0'
BAUD          = int(sys.argv[2]) if len(sys.argv) > 2 else 115200
TARGET_RPM    = float(sys.argv[3]) if len(sys.argv) > 3 else 40.0
SECONDS       = float(sys.argv[4]) if len(sys.argv) > 4 else 5.0
CURRENT_SCALE = float(sys.argv[5]) if len(sys.argv) > 5 else 0.66

# Must match serial_bridge.py's constants — this is what "raw_ticks * METERS_PER_TICK"
# is actually calibrated against for the (unscaled) right wheel.
WHEEL_RADIUS    = 0.08255
WHEEL_BASE      = 0.41
POLE_PAIRS      = 10
TICKS_PER_REV   = POLE_PAIRS * 6
METERS_PER_TICK = (2 * math.pi * WHEEL_RADIUS) / TICKS_PER_REV


def read_odom(ser, timeout=0.2):
    """Read one 'O <left> <right> [gz]' line from the serial port."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        raw = ser.readline().decode('utf-8', errors='ignore').strip()
        if raw.startswith('O '):
            parts = raw.split()
            if len(parts) in (3, 4):
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

    print(f'Target RPM={TARGET_RPM}  Duration={SECONDS}s  Current LEFT_TICK_SCALE={CURRENT_SCALE}')
    print('\nMark the robot\'s exact start position + heading (e.g. tape line on the')
    print('floor along its forward direction) before continuing.')
    print('Starting in 3 seconds ...')
    for i in range(3, 0, -1):
        print(f'  {i}...', flush=True)
        time.sleep(1.0)

    ser.reset_input_buffer()

    print('\nReading baseline ...', end='', flush=True)
    l0 = r0 = None
    for _ in range(5):
        l, r = read_odom(ser, timeout=0.3)
        if l is not None:
            l0, r0 = l, r
            break
    if l0 is None:
        print('\nERROR: no odom data from ESP32. Is it running?')
        ser.close(); sys.exit(1)
    print(f' L={l0}  R={r0}')

    print(f'Driving straight at {TARGET_RPM} RPM (both wheels) for {SECONDS:.1f}s ...', flush=True)
    cmd = f'V {TARGET_RPM} {TARGET_RPM}\n'.encode()

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

    ser.write(b'S\n')
    ser.flush()
    print()

    time.sleep(0.2)
    for _ in range(5):
        l, r = read_odom(ser, timeout=0.3)
        if l is not None:
            l_end, r_end = l, r
            break

    ser.close()

    if l_end is None:
        print('ERROR: lost serial data during run.')
        sys.exit(1)

    raw_ticks_l = l_end - l0
    raw_ticks_r = r_end - r0

    if raw_ticks_l == 0 or raw_ticks_r == 0:
        print('\nERROR: one wheel produced 0 ticks — check Hall sensors / wiring.')
        sys.exit(1)

    dist_r_true = raw_ticks_r * METERS_PER_TICK           # right: unscaled, treated as reference
    dist_l_reported = raw_ticks_l * METERS_PER_TICK * CURRENT_SCALE  # what odometry currently reports for left
    d_nominal = (dist_r_true + dist_l_reported) / 2.0

    print('\n' + '─' * 60)
    print(f'  Raw ticks   — left: {raw_ticks_l:+d}   right: {raw_ticks_r:+d}')
    print(f'  Nominal forward distance travelled: ~{d_nominal:.3f} m')
    print('─' * 60)

    try:
        y_cm = float(input(
            '\nMeasure the sideways drift from the straight line, in cm\n'
            '(positive = robot drifted RIGHT, negative = drifted LEFT): '
        ))
    except ValueError:
        print('Not a number — aborting.')
        sys.exit(1)

    y_m = y_cm / 100.0

    # Small-angle circular-arc approximation: heading drift theta ≈ 2*Y/D,
    # and theta ≈ (true_dist_R - true_dist_L) / WHEEL_BASE for a diff-drive
    # robot — combine to solve for the TRUE left distance independent of
    # whatever scale was already baked into the odometry that produced
    # dist_l_reported above.
    implied_diff = (2.0 * y_m * WHEEL_BASE) / d_nominal   # true_dist_R - true_dist_L
    true_dist_l = dist_r_true - implied_diff
    raw_dist_l_unscaled = raw_ticks_l * METERS_PER_TICK    # what left ticks give at scale=1.0

    if raw_dist_l_unscaled == 0:
        print('ERROR: degenerate left-tick distance, cannot solve for scale.')
        sys.exit(1)

    new_scale = true_dist_l / raw_dist_l_unscaled

    print('\n' + '═' * 60)
    print(f'  Measured drift: {y_cm:+.1f} cm over {d_nominal:.3f} m forward')
    if abs(y_cm) < 1.0:
        print('  ✓  Drift is within ~1cm — current scale is already good.')
    else:
        side = 'right' if y_cm > 0 else 'left'
        print(f'  ✗  Robot curves {side} — current LEFT_TICK_SCALE is off.')
    print()
    print(f'  Current LEFT_TICK_SCALE : {CURRENT_SCALE:.4f}')
    print(f'  Corrected LEFT_TICK_SCALE: {new_scale:.4f}')
    print()
    print('  ── Apply the fix ────────────────────────────────────────────────')
    print(f'  1. In esp32_motor_controller.ino, set:')
    print(f'         #define LEFT_TICK_SCALE {new_scale:.4f}f')
    print('  2. Re-flash the ESP32.')
    print(f'  3. Re-run this script (pass {new_scale:.4f} as the 5th argument)')
    print('     to verify — drift should now measure close to 0 cm.')
    print('  (Also update the left_tick_scale ROS param in your launch')
    print('   scripts to the same value, so /wheel_odom stays consistent')
    print('   with the corrected physical behaviour — that one only affects')
    print('   reported odometry, not actual driving, but should still match.)')
    print('═' * 60)


if __name__ == '__main__':
    main()
