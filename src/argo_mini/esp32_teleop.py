#!/usr/bin/env python3
"""
Argo Mini — Full ESP32 Teleop (no ROS)
Pairs with esp32_simple.ino.

Usage:  python3 esp32_teleop.py [port] [baud]

    W  — forward          X  — backward
    A  — tank-left        D  — tank-right
    Z  — back-left arc    C  — back-right arc
    S  — STOP
    +  — speed up         -  — speed down   (DAC 104 → 120)
    Ctrl+C  quit
"""

import sys, time, termios, tty, threading, serial

PORT = sys.argv[1] if len(sys.argv) > 1 else '/dev/ttyUSB1'
BAUD = int(sys.argv[2]) if len(sys.argv) > 2 else 115200

DAC_MIN = 104
DAC_MAX = 120

dac   = DAC_MIN   # current speed level
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
                elif ch == 'w':
                    dac_l, dac_r = +d, +d;  label = 'FORWARD'
                elif ch == 'x':
                    dac_l, dac_r = -d, -d;  label = 'BACKWARD'
                elif ch == 'a':
                    dac_l, dac_r = -d, +d;  label = 'TANK LEFT'
                elif ch == 'd':
                    dac_l, dac_r = +d, -d;  label = 'TANK RIGHT'
                elif ch == 'z':
                    dac_l, dac_r =  0, -d;  label = 'BACK-LEFT'
                elif ch == 'c':
                    dac_l, dac_r = -d,  0;  label = 'BACK-RIGHT'
                elif ch == 's':
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
    print('┌─────────────────────────────────────────────┐')
    print('│      ARGO MINI  —  ESP32 Full Teleop        │')
    print('├──────────────┬──────────────┬───────────────┤')
    print('│              │  W  forward  │               │')
    print('│  A tank-left │  S  stop     │ D tank-right  │')
    print('│              │  X  backward │               │')
    print('│  Z back-left │              │ C back-right  │')
    print('├──────────────┴──────────────┴───────────────┤')
    print('│   +  speed up   -  speed down   Ctrl+C quit │')
    print('└─────────────────────────────────────────────┘')
    print()
    print()
    print()

    try:
        while not quit_.is_set():
            with lock:
                l, r, d, lbl = dac_l, dac_r, dac, label
            pct = int((d - DAC_MIN) / (DAC_MAX - DAC_MIN) * 20)
            spd_bar = '█' * pct + '·' * (20 - pct)
            print(
                f'\033[3A'
                f'\r  [{lbl:<12s}]  L={l:+4d}  R={r:+4d}\n'
                f'\r  Speed: [{spd_bar}] DAC {d}/{DAC_MAX}\n'
                f'\r',
                end='', flush=True
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
