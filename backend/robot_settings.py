"""Runtime-adjustable robot settings: nav speed limits and speaker/mic volume.

Values are persisted to backend/robot_settings.json so they survive restarts.
Speed limits are pushed live to Nav2 with `ros2 param set` (MPPI and the
velocity smoother both accept dynamic updates), and re-applied after the nav
stack starts. Volume goes through ALSA's amixer.
"""
import json
import os
import re
import shlex
import subprocess
import threading
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
SETTINGS_PATH = os.path.join(_HERE, 'robot_settings.json')

# Hard ranges the UI sliders expose. Upper bounds are the limits we are
# willing to let a user pick, not what the hardware can do.
RANGES = {
    'max_linear':  {'min': 0.1, 'max': 0.8, 'step': 0.05, 'unit': 'm/s'},
    'max_angular': {'min': 0.2, 'max': 1.2, 'step': 0.05, 'unit': 'rad/s'},
    'speaker':     {'min': 0,   'max': 100, 'step': 1,    'unit': '%'},
    'mic':         {'min': 0,   'max': 100, 'step': 1,    'unit': '%'},
}
DEFAULTS = {'max_linear': 0.5, 'max_angular': 0.6}   # match nav2.yaml

# On the Jetson the USB audio card isn't ALSA's default card.
_ON_JETSON = os.path.exists('/etc/nv_tegra_release') or 'argo' in os.uname().nodename.lower()
_CARD_ARGS = ['-c', 'Device'] if _ON_JETSON else []

_lock = threading.Lock()


def _load():
    try:
        with open(SETTINGS_PATH) as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(data):
    tmp = SETTINGS_PATH + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, SETTINGS_PATH)


def _clamp(key, value):
    r = RANGES[key]
    return max(r['min'], min(r['max'], float(value)))


# ── Audio ────────────────────────────────────────────────────────────────────

def _amixer(*args):
    return subprocess.run(['amixer', *_CARD_ARGS, *args], capture_output=True, text=True, timeout=5)


def _read_percent(control, capture=False):
    try:
        out = _amixer('sget', control).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    # Use the capture line for the mic, the playback line for the speaker.
    pat = r'Capture \d+ \[(\d+)%\]' if capture else r'Playback \d+ \[(\d+)%\]'
    m = re.search(pat, out)
    return int(m.group(1)) if m else None


def get_audio():
    return {'speaker': _read_percent('Speaker'), 'mic': _read_percent('Mic', capture=True)}


def set_audio(speaker=None, mic=None):
    errors = []
    if speaker is not None:
        v = int(_clamp('speaker', speaker))
        r = _amixer('sset', 'Speaker', f'{v}%', 'unmute')
        if r.returncode != 0:
            errors.append(f'speaker: {r.stderr.strip()}')
    if mic is not None:
        v = int(_clamp('mic', mic))
        r = _amixer('sset', 'Mic', 'cap', f'{v}%')
        if r.returncode != 0:
            errors.append(f'mic: {r.stderr.strip()}')
    return errors


# ── Motion ───────────────────────────────────────────────────────────────────

def _ros(cmd):
    full = (
        'source /opt/ros/humble/setup.bash && '
        f'source {shlex.quote(_ROOT)}/install/setup.bash && '
        'export RMW_IMPLEMENTATION=rmw_cyclonedds_cpp && ' + cmd
    )
    return subprocess.run(['bash', '-c', full], capture_output=True, text=True, timeout=20)


def _apply_motion_once(lin, ang):
    # Angular accel scales with the speed so a higher limit still ramps up
    # in about a second (same ratio as the 0.6 rad/s / 0.5 rad/s^2 tuning).
    az = round(ang * 0.83, 3)
    sets = [
        ('/controller_server', 'FollowPath.vx_max', lin),
        ('/controller_server', 'FollowPath.wz_max', ang),
        ('/controller_server', 'FollowPath.wz_std', round(ang * 1.5, 3)),
        ('/controller_server', 'FollowPath.az_max', az),
        ('/velocity_smoother', 'max_velocity', f'[{lin}, 0.0, {ang}]'),
        ('/velocity_smoother', 'min_velocity', f'[{-lin}, 0.0, {-ang}]'),
        ('/velocity_smoother', 'max_accel', f'[0.2, 0.0, {az}]'),
        ('/velocity_smoother', 'max_decel', f'[-0.4, 0.0, {-az}]'),
        ('/behavior_server', 'max_rotational_vel', ang),
    ]
    cmd = ' && '.join(f'ros2 param set {n} {p} {shlex.quote(str(v))}' for n, p, v in sets)
    r = _ros(cmd)
    return r.returncode == 0, (r.stderr or r.stdout).strip()[-300:]


def apply_motion(retry_for_s=0.0):
    """Push saved limits to the running nav stack. With retry_for_s > 0, keeps
    trying until the nodes appear (used right after the stack is started)."""
    s = get_settings()
    lin, ang = s['max_linear'], s['max_angular']
    deadline = time.time() + retry_for_s
    while True:
        try:
            ok, msg = _apply_motion_once(lin, ang)
        except (OSError, subprocess.SubprocessError) as e:
            ok, msg = False, str(e)
        if ok or time.time() >= deadline:
            return ok, msg
        time.sleep(5)


def apply_motion_after_start():
    """Non-blocking: wait for Nav2 to come up, then apply saved limits.
    Skipped when the user never changed them (nav2.yaml already matches)."""
    if 'max_linear' not in _load() and 'max_angular' not in _load():
        return
    threading.Thread(target=apply_motion, kwargs={'retry_for_s': 120.0}, daemon=True).start()


# ── Public API ───────────────────────────────────────────────────────────────

def get_settings():
    with _lock:
        saved = _load()
    return {
        'max_linear':  _clamp('max_linear',  saved.get('max_linear',  DEFAULTS['max_linear'])),
        'max_angular': _clamp('max_angular', saved.get('max_angular', DEFAULTS['max_angular'])),
    }


def snapshot():
    return {**get_settings(), **{k: v for k, v in get_audio().items()}, 'ranges': RANGES}


def update_motion(max_linear=None, max_angular=None):
    """Persist and apply. Returns (applied_live, message)."""
    with _lock:
        saved = _load()
        if max_linear is not None:
            saved['max_linear'] = _clamp('max_linear', max_linear)
        if max_angular is not None:
            saved['max_angular'] = _clamp('max_angular', max_angular)
        _save(saved)
    return apply_motion()
