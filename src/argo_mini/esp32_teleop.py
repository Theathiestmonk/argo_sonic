#!/usr/bin/env python3
"""
Argo Mini — Simple ESP32 DAC Teleop
No ROS. No kinematics. Just raw DAC values over serial.

Usage:  python3 esp32_teleop.py [port] [baud]

Controls:
  W   forward          S   reverse
  A   arc left         D   arc right
  SPACE  stop
  +   increase speed   -   decrease speed
  Ctrl+C  quit
"""

import sys, time, termios, tty, threading, serial

PORT  = sys.argv[1] if len(sys.argv) > 1 else '/dev/ttyUSB1'
BAUD  = int(sys.argv[2]) if len(sys.argv) > 2 else 115200

DAC_LEVELS = [104, 105, 106, 107, 108]   # low → high speed
dac_idx    = 0                             # start at lowest speed

dac_l = 0
dac_r = 0
lock  = threading.Lock()
quit_ = threading.Event()


def send(ser):
    """20 Hz command publisher."""
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
    global dac_l, dac_r, dac_idx
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
                elif ch in ('w', '\x1b[A'):   # forward
                    dac_l, dac_r =  dac,  dac
                elif ch in ('s', '\x1b[B'):   # reverse
                    dac_l, dac_r = -dac, -dac
                elif ch in ('a', '\x1b[D'):   # arc left
                    dac_l, dac_r =    0,  dac
                elif ch in ('d', '\x1b[C'):   # arc right
                    dac_l, dac_r =  dac,    0
                elif ch == ' ':               # stop
                    dac_l, dac_r =    0,    0
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

    threading.Thread(target=send, args=(ser,), daemon=True).start()
    threading.Thread(target=read_keys, daemon=True).start()

    print('\033[2J\033[H', end='')
    print('  ARGO MINI — ESP32 Teleop')
    print('  W fwd  S rev  A left  D right  SPACE stop  +/- speed  Ctrl+C quit')
    print()

    try:
        while not quit_.is_set():
            with lock:
                l, r, idx = dac_l, dac_r, dac_idx
            spd = DAC_LEVELS[idx]
            print(f'\r  DAC L={l:+4d}  R={r:+4d}   speed level {idx+1}/{len(DAC_LEVELS)} (DAC {spd})   ',
                  end='', flush=True)
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
