#!/usr/bin/env python3
"""
Argo Mini — Simple ESP32 Teleop (no ROS, no tank turns)
Pairs with esp32_simple.ino.

Usage:  python3 esp32_teleop.py [port] [baud]

    W  — forward
    A  — arc left  (left stops, right drives)
    D  — arc right (right stops, left drives)
    X  — reverse
    S  — STOP
    +  — speed up   (DAC 104 → 120)
    -  — speed down
    Ctrl+C  quit
"""

import sys, time, termios, tty, threading, serial

PORT = sys.argv[1] if len(sys.argv) > 1 else '/dev/ttyUSB1'
BAUD = int(sys.argv[2]) if len(sys.argv) > 2 else 115200

DAC_MIN = 104
DAC_MAX = 120

dac   = DAC_MIN
dac_l = 0
dac_r = 0
label = 'STOP'
lock  = threading.Lock()
quit_ = threading.Event()


def send(ser):
    while not quit_.is_set():
        with lock:
            l, r = dac_l, dac_r
        try:
            ser.write(f'V {l} {r}\n'.encode())
            ser.flush()
        except Exception:
            break
        time.sleep(0.05)


def read_keys():
    global dac, dac_l, dac_r, label
    fd  = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    tty.setraw(fd)
    try:
        while not quit_.is_set():
            ch = sys.stdin.read(1)
            if ch == '\x1b':
                try: ch += sys.stdin.read(2)
                except: pass

            with lock:
                d = dac
                if ch in ('\x03', '\x04'):
                    quit_.set()
                elif ch in ('w', '\x1b[A'):
                    dac_l, dac_r = +d, +d;  label = 'FORWARD'
                elif ch == 'x':
                    dac_l, dac_r = -d, -d;  label = 'REVERSE'
                elif ch in ('a', '\x1b[D'):
                    dac_l, dac_r =  0, +d;  label = 'ARC LEFT'
                elif ch in ('d', '\x1b[C'):
                    dac_l, dac_r = +d,  0;  label = 'ARC RIGHT'
                elif ch in ('s', ' '):
                    dac_l, dac_r =  0,  0;  label = 'STOP'
                elif ch in ('+', '='):
                    dac = min(dac + 1, DAC_MAX)
                elif ch == '-':
                    dac = max(dac - 1, DAC_MIN)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def main():
    try:
        ser = serial.Serial(PORT, BAUD, timeout=0.1)
    except serial.SerialException as e:
        print(f'Cannot open {PORT}: {e}'); sys.exit(1)

    time.sleep(1.5)
    ser.reset_input_buffer()

    threading.Thread(target=send,      args=(ser,), daemon=True).start()
    threading.Thread(target=read_keys, daemon=True).start()

    print('\033[2J\033[H', end='')
    print('┌──────────────────────────────────────┐')
    print('│    ARGO MINI  —  Simple Teleop       │')
    print('├──────────────────────────────────────┤')
    print('│         W / ↑   forward              │')
    print('│  A / ←  arc-left   D / →  arc-right │')
    print('│         X        reverse             │')
    print('│         S / SPC  stop                │')
    print('│         + / -    speed               │')
    print('│         Ctrl+C   quit                │')
    print('└──────────────────────────────────────┘')
    print()
    print()
    print()

    try:
        while not quit_.is_set():
            with lock:
                l, r, d, lbl = dac_l, dac_r, dac, label
            pct     = int((d - DAC_MIN) / (DAC_MAX - DAC_MIN) * 20)
            spd_bar = '█' * pct + '·' * (20 - pct)
            print(
                f'\033[3A'
                f'\r  [{lbl:<10s}]  L={l:+4d}  R={r:+4d}\n'
                f'\r  Speed: [{spd_bar}] DAC {d}/{DAC_MAX}\n'
                f'\r',
                end='', flush=True,
            )
            time.sleep(0.1)
    except KeyboardInterrupt:
        pass
    finally:
        try: ser.write(b'S\n'); ser.flush()
        except: pass
        ser.close()
        print('\nStopped.')


if __name__ == '__main__':
    main()
