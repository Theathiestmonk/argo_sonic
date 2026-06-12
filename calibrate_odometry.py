#!/usr/bin/env python3
"""
Argo Mini ? Wheel Odometry Calibration

Stop serial_bridge before running (it holds the serial port).
Push the robot a known distance on flat ground.
The tool counts Hall ticks and prints corrected WHEEL_RADIUS
and left_tick_scale to paste into serial_bridge.py / start script.

Usage:
    python3 calibrate_odometry.py
    python3 calibrate_odometry.py /dev/ttyUSB1
"""

import math
import sys
import threading
import time

import serial

# ?? Current values in serial_bridge.py (for comparison) ?????????????????????
WHEEL_RADIUS_NOW  = 0.0762
POLE_PAIRS        = 10
TICKS_PER_REV     = POLE_PAIRS * 6          # 90
M_PER_TICK_NOW    = (2 * math.pi * WHEEL_RADIUS_NOW) / TICKS_PER_REV

PORT = sys.argv[1] if len(sys.argv) > 1 else '/dev/ttyUSB1'
BAUD = 115200

# ?? Shared serial state ??????????????????????????????????????????????????????
_left        = 0
_right       = 0
_lock        = threading.Lock()
_stop_reader = False
_stop_live   = False


def _serial_reader(ser):
    global _left, _right, _stop_reader
    while not _stop_reader:
        try:
            line = ser.readline().decode('utf-8', errors='ignore').strip()
            if line.startswith('O '):
                parts = line.split()
                if len(parts) == 3:
                    with _lock:
                        _left  = int(parts[1])
                        _right = int(parts[2])
        except Exception:
            pass


def _live_display(start_l, start_r):
    global _stop_live
    while not _stop_live:
        with _lock:
            dl = _left  - start_l
            dr = _right - start_r
        est = dr * M_PER_TICK_NOW
        print(
            f'\r  Left: {dl:7d} ticks   Right: {dr:7d} ticks'
            f'   Est distance: {est:.4f} m     ',
            end='', flush=True
        )
        time.sleep(0.05)


def _banner(title):
    print('\n' + '=' * 62)
    print(f'  {title}')
    print('=' * 62)


def _print_results(distance_m, dl, dr):
    _banner('RESULTS')

    if dr == 0 or dl == 0:
        print('  ERROR: Zero ticks recorded ? was the robot actually moved?')
        return

    mpt_r     = distance_m / dr
    mpt_l     = distance_m / dl
    r_right   = (mpt_r * TICKS_PER_REV) / (2 * math.pi)
    r_left    = (mpt_l * TICKS_PER_REV) / (2 * math.pi)
    new_scale = dr / dl

    pct_r = (r_right - WHEEL_RADIUS_NOW) / WHEEL_RADIUS_NOW * 100
    pct_s = (new_scale - 2.1714) / 2.1714 * 100

    print(f'  Distance you pushed   : {distance_m:.4f} m')
    print(f'  Left  ticks counted   : {dl}')
    print(f'  Right ticks counted   : {dr}')
    print()
    print(f'  ?? Right wheel (primary calibration) ??????????????????')
    print(f'  m/tick  : {mpt_r:.7f}   (was {M_PER_TICK_NOW:.7f})')
    print(f'  WHEEL_RADIUS : {r_right:.5f} m   '
          f'(was {WHEEL_RADIUS_NOW:.5f} m,  {pct_r:+.1f}%)')
    print()
    print(f'  ?? Left wheel ?????????????????????????????????????????')
    print(f'  m/tick  : {mpt_l:.7f}')
    print(f'  WHEEL_RADIUS : {r_left:.5f} m')
    print()
    print(f'  ?? left_tick_scale ????????????????????????????????????')
    print(f'  new value : {new_scale:.4f}   (was 2.1714,  {pct_s:+.1f}%)')
    print()
    print('  ?? Paste into serial_bridge.py ????????????????????????')
    print(f'  WHEEL_RADIUS  = {r_right:.5f}')
    print()
    print('  ?? Paste into start_argo_nav.sh ???????????????????????')
    print(f'  -p left_tick_scale:={new_scale:.4f}')
    print('=' * 62)


def main():
    global _stop_reader, _stop_live

    _banner('ARGO MINI ? Odometry Calibration')
    print(f'  Port                 : {PORT}')
    print(f'  Current WHEEL_RADIUS : {WHEEL_RADIUS_NOW:.5f} m')
    print(f'  Current TICKS_PER_REV: {TICKS_PER_REV}')
    print(f'  Current m/tick       : {M_PER_TICK_NOW:.7f} m')
    print('=' * 62)
    print('  IMPORTANT: stop serial_bridge before running this tool.')
    print()

    # ?? Open serial port ?????????????????????????????????????????????????????
    try:
        ser = serial.Serial(PORT, BAUD, timeout=0.1)
        time.sleep(2.0)
        ser.reset_input_buffer()
        print(f'  Connected to {PORT} at {BAUD} baud.')
    except serial.SerialException as e:
        print(f'\n  ERROR: Cannot open {PORT}: {e}')
        print('  Run:  sudo chmod 666 /dev/ttyUSB1')
        sys.exit(1)

    t_reader = threading.Thread(target=_serial_reader, args=(ser,), daemon=True)
    t_reader.start()

    time.sleep(0.3)   # let reader warm up

    # ?? START ?????????????????????????????????????????????????????????????????
    print()
    print('  Place the robot at your START mark.')
    print()
    input('  >>>  Press ENTER to START counting ticks  <<<')
    print()

    with _lock:
        start_l = _left
        start_r = _right

    print(f'  Tick baseline captured:  L={start_l}   R={start_r}')
    print()
    print('  Now push the robot forward to the END mark.')
    print('  Keep it straight. Recommended distance: 1.00 m')
    print()
    print('  >>>  Press ENTER to STOP counting ticks  <<<')
    print()

    _stop_live = False
    t_live = threading.Thread(target=_live_display, args=(start_l, start_r), daemon=True)
    t_live.start()

    input()   # wait for STOP

    _stop_live = True
    t_live.join()
    print()

    with _lock:
        end_l = _left
        end_r = _right

    dl = end_l - start_l
    dr = end_r - start_r

    print(f'  Final ticks:  L={end_l} (?{dl})   R={end_r} (?{dr})')
    print()

    # ?? Ask actual distance ??????????????????????????????????????????????????
    raw = input('  Enter actual distance pushed in metres [1.0]: ').strip()
    distance = float(raw) if raw else 1.0

    # ?? Cleanup ???????????????????????????????????????????????????????????????
    _stop_reader = True
    ser.close()

    _print_results(distance, dl, dr)


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print('\n\n  Aborted.')
