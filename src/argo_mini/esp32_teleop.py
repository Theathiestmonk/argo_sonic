#!/usr/bin/env python3
"""
Argo Mini — Full ESP32 Teleop (no ROS)
Raw DAC over serial. Pairs with esp32_simple.ino.

Usage:  python3 esp32_teleop.py [port] [baud]

Key layout:
             W  — forward  (both wheels forward)
    A  — tank-left         D  — tank-right
    (left back, right fwd) (left fwd, right back)
             S  — STOP
             X  — backward (both wheels reverse)
    Z  — back-left         C  — back-right
    (left stop, right rev) (left rev, right stop)

    + / -   raise / lower speed level
    Ctrl+C  quit (sends stop first)

Command held until a new key is pressed.  Press S to stop.
"""

import sys, time, termios, tty, threading, serial

PORT  = sys.argv[1] if len(sys.argv) > 1 else '/dev/ttyUSB1'
BAUD  = int(sys.argv[2]) if len(sys.argv) > 2 else 115200

DAC_LEVELS = [104, 105, 106, 107, 108]
dac_idx    = 0

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
    global dac_l, dac_r, dac_idx, label
    fd  = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    tty.setraw(fd)
    try:
        while not quit_.is_set():
            ch = sys.stdin.read(1)
            if ch == '\x1b':
                try: ch += sys.stdin.read(2)
                except: pass

            dac = DAC_LEVELS[dac_idx]

            with lock:
                if ch in ('\x03', '\x04'):
                    quit_.set()
                elif ch == 'w':               # forward
                    dac_l, dac_r =  dac,  dac
                    label = 'FORWARD'
                elif ch == 'x':               # backward
                    dac_l, dac_r = -dac, -dac
                    label = 'BACKWARD'
                elif ch == 'a':               # tank left
                    dac_l, dac_r = -dac,  dac
                    label = 'TANK LEFT'
                elif ch == 'd':               # tank right
                    dac_l, dac_r =  dac, -dac
                    label = 'TANK RIGHT'
                elif ch == 'z':               # back-left arc
                    dac_l, dac_r =    0, -dac
                    label = 'BACK-LEFT'
                elif ch == 'c':               # back-right arc
                    dac_l, dac_r = -dac,    0
                    label = 'BACK-RIGHT'
                elif ch == 's':               # stop
                    dac_l, dac_r =    0,    0
                    label = 'STOP'
                elif ch in ('+', '='):
                    dac_idx = min(dac_idx + 1, len(DAC_LEVELS) - 1)
                elif ch == '-':
                    dac_idx = max(dac_idx - 1, 0)
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
    print('├─────────────────────────────────────────────┤')
    print('│          W  forward                          │')
    print('│  A tank-left   S stop   D tank-right        │')
    print('│          X  backward                         │')
    print('│  Z back-left            C back-right        │')
    print('│          +/-  speed     Ctrl+C quit          │')
    print('└─────────────────────────────────────────────┘')
    print()

    try:
        while not quit_.is_set():
            with lock:
                l, r, idx, lbl = dac_l, dac_r, dac_idx, label
            spd = DAC_LEVELS[idx]
            print(
                f'\r  [{lbl:<12s}]  L={l:+4d}  R={r:+4d}'
                f'   speed {idx+1}/{len(DAC_LEVELS)} (DAC {spd})   ',
                end='', flush=True
            )
            time.sleep(0.1)
    except KeyboardInterrupt:
        pass
    finally:
        try: ser.write(b'S\n'); ser.flush()
        except: pass
        ser.close()
        print('\n\nStopped.')


if __name__ == '__main__':
    main()
