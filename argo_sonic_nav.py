#!/usr/bin/env python3
"""
Argo Sonic – NTFields Navigation Launcher
Usage: python3 argo_sonic_nav.py [--no-cam] [--map /path/to/map]
"""

import os, sys, re, time, signal, shutil, subprocess, threading, argparse, io, math, select, json, yaml
from datetime import datetime
from pathlib import Path

WHEEL_RADIUS = 0.0762
WHEEL_BASE   = 0.41

# Where the robot's last localized pose gets saved on shutdown and read
# back on the next start — see save_last_pose()/seed_initial_pose() below.
# /tmp, not somewhere in the repo, since a genuine reboot (not just a
# Stop/Start Argo cycle) makes a saved pose no more trustworthy than not
# having one — if the robot got physically moved while powered off, this
# should NOT survive that.
LAST_POSE_PATH = "/tmp/argo_last_pose.json"
# A saved pose older than this is more likely to reflect the robot having
# been moved by hand since than to still be accurate — skip seeding rather
# than confidently feed slam_toolbox a stale guess.
POSE_MAX_AGE_S = 3600

# Repo root, derived from this file's own location — not hardcoded to
# ~/my_project/argo_sonic, since this checkout can (and on the actual robot, does)
# live somewhere else, e.g. ~/my_project/argo_sonic. Matches the sh/*.sh
# scripts' own SCRIPT_DIR convention for the same reason.
REPO_ROOT = str(Path(__file__).resolve().parent)

if hasattr(sys.stdout, 'buffer'):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
os.environ['PYTHONIOENCODING'] = 'utf-8'

# ──────────────────────────────────────────────────────────────────────────────
#  ANSI color system
# ──────────────────────────────────────────────────────────────────────────────
def rgb(r, g, b):  return f"\033[38;2;{r};{g};{b}m"
def bgc(r, g, b):  return f"\033[48;2;{r};{g};{b}m"
RS = "\033[0m"; BLD = "\033[1m"; DIM = "\033[2m"

GOLD    = rgb(255, 200,   0)
GOLD2   = rgb(220, 150,   0)
GOLD3   = rgb( 80,  50,   0)
GOLD_BG = bgc( 18,  13,   0)
GREEN   = rgb( 50, 220,  90)
RED     = rgb(255,  65,  65)
CYAN    = rgb(  0, 200, 240)
YELLOW  = rgb(255, 210,  50)
WHITE   = rgb(210, 210, 215)
GRAY    = rgb( 85,  85,  95)
PURPLE  = rgb(165,  95, 255)
ORANGE  = rgb(255, 140,  40)
BORDER  = rgb( 75,  55, 145)
BG_H    = bgc( 11,   9,  20)
BG_L    = bgc(  7,   7,  12)

STRIP_RE  = re.compile(r'\033\[[^m]*m')
_RE_CMD   = re.compile(r'lin=([-\d.]+).*ang=([-\d.]+)')
_RE_RPM   = re.compile(r'Sending: V ([-\d.]+) ([-\d.]+)')
def vis(s):  return STRIP_RE.sub('', s)
def vlen(s): return len(vis(s))

SPIN = ["|", "/", "-", "\\"]

def hline(w=80): return f"{BORDER}+{'-'*(w-2)}+{RS}"
def top(w):      return f"{BORDER}+{'-'*(w-2)}+{RS}"
def bot(w):      return f"{BORDER}+{'-'*(w-2)}+{RS}"

def row(content="", bg=BG_L, w=80):
    pad = " " * max(0, w - 2 - vlen(content))
    return f"{BORDER}|{RS}{bg}{content}{pad}{RS}{BORDER}|{RS}"

# ──────────────────────────────────────────────────────────────────────────────
#  Global state
# ──────────────────────────────────────────────────────────────────────────────
pids       : dict = {}
# Mutated (not reassigned) after build_env(home) runs in main(), so
# cleanup() — a signal handler with no direct access to main()'s locals —
# can still shell out with the correct sourced ROS environment to capture
# the last pose. Empty until then; save_last_pose() no-ops safely on that.
_last_env  : dict = {}
log_lines  : list = []
log_lock   = threading.Lock()
ui_lock    = threading.Lock()
step_idx   = 0
step_name  = "Initializing..."
spin_frame = 0
stop_ui    = threading.Event()

telem      = {"lin": 0.0, "ang": 0.0, "rpm_l": 0.0, "rpm_r": 0.0}
telem_lock = threading.Lock()

TOTAL_STEPS = 14

STEP_NAMES = [
    "Robot State Publisher", "Camera TF Bridge",    "Serial Bridge",
    "RPLidar A1",            "Scan Relay",           "SLAM Toolbox",
    "NTFields Planner",      "Controller Server",   "Velocity Smoother",
    "Behavior Server",       "BT Navigator",        "Depth Camera",
    "PC Restamper",          "Safety Shield",
]

# ──────────────────────────────────────────────────────────────────────────────
#  Logger
# ──────────────────────────────────────────────────────────────────────────────
ICONS = {
    "ok":   f"{GREEN}[OK]{RS}",
    "fail": f"{RED}[!!]{RS}",
    "info": f"{CYAN}[..]{RS}",
    "warn": f"{YELLOW}[??]{RS}",
    "run":  f"{GOLD}[>>]{RS}",
    "sys":  f"{PURPLE}[##]{RS}",
}
COLORS = {
    "ok": GREEN, "fail": RED, "info": WHITE,
    "warn": YELLOW, "run": GOLD, "sys": PURPLE,
}

def _telem_reader(proc):
    for raw in proc.stdout:
        try:
            line = raw.decode('utf-8', errors='ignore')
        except Exception:
            continue
        m = _RE_CMD.search(line)
        if m:
            with telem_lock:
                telem['lin'] = float(m.group(1))
                telem['ang'] = float(m.group(2))
            continue
        m = _RE_RPM.search(line)
        if m:
            with telem_lock:
                telem['rpm_l'] = float(m.group(1))
                telem['rpm_r'] = float(m.group(2))

PROGRESS_FILE = "/tmp/argo_nav_progress"
_progress_locked = False

def report_progress(status, message):
    global _progress_locked
    if status in ("READY", "ERROR"):
        _progress_locked = True
    try:
        with open(PROGRESS_FILE, "w") as f:
            f.write(f"{status}|{int(time.time())}|{message}\n")
    except OSError:
        pass

def log(msg, kind="info"):
    ts    = datetime.now().strftime("%H:%M:%S")
    icon  = ICONS.get(kind, ICONS["info"])
    color = COLORS.get(kind, WHITE)
    line  = f"  {DIM}{GRAY}{ts}{RS}  {icon}  {color}{msg}{RS}"
    with log_lock:
        log_lines.append(line)
    if kind == "fail":
        report_progress("ERROR", msg)
    elif kind == "run" and not _progress_locked:
        report_progress("OK", msg)

# ──────────────────────────────────────────────────────────────────────────────
#  UI drawing
# ──────────────────────────────────────────────────────────────────────────────
LOGO = [
    f"  {GOLD}{BLD}  ARGO SONIC  –  NTFields Navigation  {RS}",
]

def draw_bar(idx, total, w):
    bar_w  = max(8, w - 34)
    done   = min(idx, total)
    filled = int(bar_w * done / max(total, 1))
    empty  = bar_w - filled
    fill_str = f"{BLD}{GOLD}{'#' * filled}{GOLD3}{'-' * empty}{RS}"
    pct   = f"{BLD}{GOLD}{done*100//max(total,1):3d}%{RS}"
    frac  = f"{DIM}{GRAY}{done}/{total}{RS}"
    label = f"{BLD}{GOLD}LOADING{RS}"
    return f"  {label}  [{fill_str}]  {pct}  {frac}"

def redraw():
    global spin_frame
    w  = min(shutil.get_terminal_size((80, 24)).columns, 120)
    th = shutil.get_terminal_size((80, 24)).lines
    spin_frame = (spin_frame + 1) % len(SPIN)
    spinner    = f"{GOLD}{SPIN[spin_frame]}{RS}"

    out = []
    out.append(top(w))
    out.append(row("", BG_H, w))
    for ll in LOGO:
        out.append(row(f"   {ll}", BG_H, w))
    out.append(row("", BG_H, w))
    sub = f"  {DIM}{GOLD}----  P H Y S I C S - I N F O R M E D   P L A N N E R  ----{RS}"
    out.append(row(sub, BG_H, w))
    out.append(row("", BG_H, w))
    out.append(hline(w=w))
    out.append(row(draw_bar(step_idx, TOTAL_STEPS, w), BG_H, w))
    cur = f"  {DIM}{GRAY}Active:{RS}  {spinner}  {GOLD}{step_name}{RS}"
    out.append(row(cur, BG_H, w))
    out.append(hline(w=w))

    with telem_lock:
        t = dict(telem)
    trow = (
        f"  {CYAN}Vel:{RS}"
        f"  Lin {GOLD}{t['lin']:+.3f}{RS} m/s"
        f"  Ang {GOLD}{t['ang']:+.3f}{RS} r/s"
        f"  {DIM}{GRAY}|{RS}"
        f"  {PURPLE}RPM:{RS}"
        f"  L {GOLD}{t['rpm_l']:+.1f}{RS}"
        f"  R {GOLD}{t['rpm_r']:+.1f}{RS}"
    )
    out.append(row(trow, BG_H, w))
    out.append(hline(w=w))
    out.append(row(f"  {PURPLE}{BLD}# SYSTEM LOG{RS}", BG_L, w))
    out.append(hline(w=w))

    header_rows = len(out) + 2
    log_area    = max(1, th - header_rows)
    with log_lock:
        visible = log_lines[-log_area:] if len(log_lines) > log_area else log_lines[:]
    for ll in visible:
        out.append(row(ll, BG_L, w))
    for _ in range(log_area - len(visible)):
        out.append(row("", BG_L, w))
    out.append(bot(w))

    with ui_lock:
        sys.stdout.write("\033[H")
        sys.stdout.write("\n".join(out))
        sys.stdout.write("\033[?25l")
        sys.stdout.flush()

def ui_loop():
    while not stop_ui.is_set():
        redraw()
        time.sleep(0.08)

# ──────────────────────────────────────────────────────────────────────────────
#  ROS environment
# ──────────────────────────────────────────────────────────────────────────────
def build_env(home):
    ws  = REPO_ROOT
    cmd = (
        "source /opt/ros/humble/setup.bash && "
        f"source {ws}/install/setup.bash && env"
    )
    r = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True)
    env = {}
    for line in r.stdout.splitlines():
        k, _, v = line.partition("=")
        if k:
            env[k] = v
    if "PATH" not in env:
        log(f"Failed to source ROS2 workspace at {ws}/install/setup.bash "
            f"(missing or not built — run 'colcon build' there?)", "fail")
        report_progress("ERROR", f"Workspace not built: {ws}/install/setup.bash not found")
        cleanup()
    sdk = f"{home}/EaiCameraSdk_v1.2.28.20241015/demo/linux_ros/ros2"
    env["LD_LIBRARY_PATH"] = (
        env.get("LD_LIBRARY_PATH", "") +
        f":{sdk}/ascamera/libs/lib/aarch64-linux-gnu"
    )
    env["RMW_IMPLEMENTATION"] = "rmw_cyclonedds_cpp"
    return env

# ──────────────────────────────────────────────────────────────────────────────
#  Launchers & Diagnostics
# ──────────────────────────────────────────────────────────────────────────────
def launch(name, cmd, env):
    global step_name
    step_name = name
    log(f"Starting  {name}", "run")
    p = subprocess.Popen(
        cmd, shell=True, env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        preexec_fn=os.setsid,
    )
    pids[name] = p
    return p

def launch_with_telem(name, cmd, env):
    global step_name
    step_name = name
    log(f"Starting  {name}", "run")
    p = subprocess.Popen(
        cmd, shell=True, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        preexec_fn=os.setsid,
    )
    pids[name] = p
    threading.Thread(target=_telem_reader, args=(p,), daemon=True).start()
    return p

def launch_with_retry(name, cmd, env, ready_topic, attempts=3, settle=4):
    """Launch a node, and if it dies or never publishes ready_topic within
    `settle` seconds, kill it and retry with growing backoff.

    Written for the RPLidar step specifically: a fresh RPLIDAR A1's motor
    needs a moment to spin up to speed, and a start-scan command sent before
    that (or one landing on top of stale bytes left in the UART buffer from
    an immediately-preceding attempt) fails with an SDK-level timeout/comms
    error that looks identical to an actual hardware fault but isn't one —
    confirmed on this exact unit: dmesg showed no USB disconnect/reset
    around the failure, and a clean isolated retry succeeded.
    """
    global step_name
    step_name = name
    for attempt in range(1, attempts + 1):
        tag = f"  (attempt {attempt}/{attempts})" if attempts > 1 else ""
        log(f"Starting  {name}{tag}", "run")
        p = subprocess.Popen(
            cmd, shell=True, env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            preexec_fn=os.setsid,
        )
        pids[name] = p

        deadline = time.time() + settle
        while time.time() < deadline:
            if p.poll() is not None:
                break
            r = runcmd(f"ros2 topic list --no-daemon 2>/dev/null | grep -qx '{ready_topic}'", env)
            if r.returncode == 0:
                log(f"{name} ready", "ok")
                return p
            time.sleep(0.5)

        log(f"{name} did not come up cleanly (attempt {attempt}/{attempts})", "warn")
        try:
            os.killpg(os.getpgid(p.pid), signal.SIGKILL)
        except Exception:
            pass
        pids.pop(name, None)
        if attempt < attempts:
            time.sleep(2 * attempt)  # let the motor/port fully settle before respin

    log(f"FAILED to bring up {name} after {attempts} attempts", "fail")
    return None

def runcmd(cmd, env, timeout=10):
    try:
        return subprocess.run(cmd, shell=True, env=env, capture_output=True,
                              text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(cmd, returncode=124, stdout="", stderr="timed out")

def wait_topic(topic, env, timeout=30):
    log(f"Waiting for topic  {topic}", "info")
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = runcmd(f"ros2 topic list --no-daemon 2>/dev/null | grep -qx '{topic}'", env)
        if r.returncode == 0:
            log(f"Topic ready  {topic}", "ok")
            return True
        time.sleep(1)
    log(f"Timeout – topic not found: {topic}", "warn")
    return False

def wait_topic_data(topic, env, timeout=20):
    """Wait until actual messages are actively publishing on a topic."""
    log(f"Waiting for data stream on  {topic}", "info")
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = runcmd(f"timeout 3 ros2 topic echo {topic} --once --no-daemon 2>/dev/null", env, timeout=5)
        if r.returncode == 0 and r.stdout.strip():
            log(f"Data stream active  {topic}", "ok")
            return True
        time.sleep(1)
    log(f"Timeout – no data arriving on: {topic}", "warn")
    return False

def wait_tf(parent, child, env, timeout=20):
    """Poll until TF parent->child is available."""
    log(f"Waiting for TF  {parent} -> {child}", "info")
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = runcmd(
            f"timeout 2 ros2 run tf2_ros tf2_echo {parent} {child} 2>/dev/null | head -8",
            env, timeout=4
        )
        out = (r.stdout or "") + (r.stderr or "")
        if "Translation:" in out or "At time" in out:
            log(f"TF ready  {parent} -> {child}", "ok")
            return True
        time.sleep(1)
    log(f"Timeout – TF {parent} -> {child} not available", "warn")
    return False

def save_last_pose(env):
    """Captures slam_toolbox's own last localized estimate — the SAME /pose
    topic the dashboard's live map marker reads (see frontend/src/App.jsx's
    mapPoseRef) — to LAST_POSE_PATH right before shutdown, so the NEXT
    start can seed its scan-matcher from the robot's real last position
    instead of assuming a fixed spot (e.g. "Kitchen") that may not be where
    it actually is. Called first thing in cleanup(), while /pose is still
    alive — best-effort: any failure here just means the next start won't
    have a seed to work from, not a fatal error, since slam_toolbox's own
    scan-matching doesn't strictly require one (see nav.launch.py's own
    comment on this node)."""
    if not env:
        return   # cleanup() fired before build_env(home) ever ran (e.g. Ctrl-C during map selection) — nothing to capture yet
    try:
        # Try to get slam_toolbox's final localized pose from /pose
        check = runcmd("ros2 topic list --no-daemon 2>/dev/null | grep -qx '/pose'", env, timeout=2)
        if check.returncode == 0:
            for attempt in range(3):
                r = runcmd("timeout 5 ros2 topic echo /pose --once --no-daemon", env, timeout=6)
                if r.returncode == 0 and r.stdout.strip():
                    try:
                        msg = yaml.safe_load(r.stdout)
                        if not msg or "pose" not in msg:
                            raise ValueError("Invalid message structure")
                        pos = msg["pose"]["pose"]["position"]
                        ori = msg["pose"]["pose"]["orientation"]
                        with open(LAST_POSE_PATH, "w") as f:
                            json.dump({"x": pos["x"], "y": pos["y"], "qz": ori["z"], "qw": ori["w"],
                                       "saved_at": time.time()}, f)
                        log(f"Saved last pose ({pos['x']:.2f}, {pos['y']:.2f}) for next start", "ok")
                        return
                    except (ValueError, KeyError, TypeError) as e:
                        log(f"Pose parse error (attempt {attempt + 1}/3): {e}", "warn")
                if attempt < 2:
                    time.sleep(0.5)
            log("Couldn't capture /pose topic — trying odometry fallback", "warn")

        # Fallback: get last odometry position if /pose topic unavailable
        r = runcmd("timeout 3 ros2 topic echo /odom --once --no-daemon", env, timeout=4)
        if r.returncode == 0 and r.stdout.strip():
            msg = yaml.safe_load(r.stdout)
            if msg and "pose" in msg and "pose" in msg["pose"]:
                pos = msg["pose"]["pose"]["position"]
                ori = msg["pose"]["pose"]["orientation"]
                with open(LAST_POSE_PATH, "w") as f:
                    json.dump({"x": pos["x"], "y": pos["y"], "qz": ori["z"], "qw": ori["w"],
                               "saved_at": time.time()}, f)
                log(f"Saved last odometry pose ({pos['x']:.2f}, {pos['y']:.2f}) for next start", "ok")
                return

        log("Couldn't capture pose from /pose or /odom — robot position won't be seeded next start", "warn")
    except Exception as e:
        log(f"Couldn't save last pose: {e}", "warn")

def seed_initial_pose(env):
    """Publishes the pose save_last_pose() captured on the PREVIOUS
    shutdown to /initialpose, seeding slam_toolbox's scan-matcher with the
    robot's actual last position instead of leaving it to converge from
    nothing. Not strictly required (nav.launch.py's own comment: this
    node's scan-matching works fine with no initial pose at all) but helps
    convergence speed and disambiguates symmetric-looking spaces (e.g. a
    row of visually-identical tables). Silent no-op if there's nothing
    saved yet (first-ever start on this map) or it's too old to trust —
    see POSE_MAX_AGE_S. Called once, right after SLAM Toolbox itself is up
    (its /initialpose subscriber has to actually exist before this can
    reach it — a message published before that has no subscriber to catch
    it and is simply lost, standard ROS 2 pub/sub, not queued for a late
    joiner)."""
    try:
        if not os.path.isfile(LAST_POSE_PATH):
            return
        with open(LAST_POSE_PATH) as f:
            saved = json.load(f)
        age = time.time() - saved.get("saved_at", 0)
        if age > POSE_MAX_AGE_S:
            log(f"Saved pose is {age / 3600:.1f}h old — too stale to trust, skipping seed", "warn")
            return

        # Wait for /initialpose subscriber to be ready (up to 5s) before publishing
        deadline = time.time() + 5.0
        while time.time() < deadline:
            r = runcmd("ros2 topic info /initialpose --no-daemon 2>/dev/null | grep -q 'Subscription count'", env, timeout=2)
            if r.returncode == 0:
                break
            time.sleep(0.2)

        # Same covariance values RViz's own "2D Pose Estimate" publishes —
        # see frontend/src/App.jsx's sendInitialPose, kept identical so a
        # seeded pose behaves exactly like a manually-set one.
        msg = (
            '{header: {frame_id: "map"}, '
            f'pose: {{pose: {{position: {{x: {saved["x"]}, y: {saved["y"]}, z: 0.0}}, '
            f'orientation: {{x: 0.0, y: 0.0, z: {saved["qz"]}, w: {saved["qw"]}}}}}, '
            'covariance: [0.25,0,0,0,0,0, 0,0.25,0,0,0,0, 0,0,0,0,0,0, '
            '0,0,0,0,0,0, 0,0,0,0,0,0, 0,0,0,0,0,0.06853891945200942]}}'
        )

        # Retry up to 3 times with a small delay to ensure delivery
        for attempt in range(3):
            r = runcmd(f"ros2 topic pub -1 /initialpose geometry_msgs/msg/PoseWithCovarianceStamped "
                       f"'{msg}' --no-daemon 2>&1", env, timeout=5)
            if r.returncode == 0:
                log(f"Seeded initial pose from last run ({saved['x']:.2f}, {saved['y']:.2f})", "ok")
                return
            if attempt < 2:
                time.sleep(0.3)

        log(f"Couldn't publish seeded initial pose after 3 attempts: {(r.stderr or r.stdout or '')[-200:]}", "warn")
    except Exception as e:
        log(f"Couldn't seed initial pose: {e}", "warn")

def wait_nav_prerequisites(env, timeout_odom=25, timeout_scan=20, timeout_map=30, timeout_tf=45):
    """Block until odometry, scan streams, map, and TFs are strictly active.

    timeout_tf=45 (not 20): on-robot testing showed odom->base_footprint and
    base_footprint->base_link both resolve fine, just not inside 20s — the
    Jetson is already under heavy load at boot (NTFields alone can take 30s+
    to configure), so the TF tree just hasn't settled yet at the 20s mark,
    not because anything is structurally broken.
    """
    log("Checking navigation prerequisites (odom / scan / map / TF)...", "sys")
    ok = True
    
    # 1. Check for /odom topic existence and data flow
    if not wait_topic("/odom", env, timeout=timeout_odom):
        ok = False
    elif not wait_topic_data("/odom", env, timeout=10):
        log("Topic /odom exists but not publishing data", "warn")
        ok = False

    # 2. Check for scan data
    if not wait_topic("/scan", env, timeout=timeout_scan):
        log("Primary /scan not found – continuing (scan_relay may use another name)", "warn")
        
    # 3. Check for map
    if not wait_topic("/map", env, timeout=timeout_map):
        ok = False
        
    # 4. Check critical transform tree: odom -> base_link
    if not wait_tf("odom", "base_link", env, timeout=timeout_tf):
        ok = False

    if ok:
        log("Navigation prerequisites ready", "ok")
    else:
        log("Some prerequisites missing – lifecycle nodes may still fail on first try", "warn")
    return ok

def wait_action(action, env, timeout=30):
    # `ros2 action list` has no --no-daemon option and always depends on the
    # daemon, spawning one if none exists — which is exactly the dependency
    # this script otherwise avoids everywhere else (Cyclone DDS discovery
    # doesn't need it, and every other ros2 CLI call here passes
    # --no-daemon). Every rclcpp/rclpy action server auto-publishes an
    # <action>/_action/status topic, so check that via `ros2 topic list
    # --no-daemon` instead — same readiness signal, zero daemon dependency.
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = runcmd(f"ros2 topic list --no-daemon --include-hidden-topics 2>/dev/null | grep -qx '{action}/_action/status'", env)
        if r.returncode == 0:
            return True
        time.sleep(1)
    log(f"Timeout – action not found: {action}", "warn")
    return False

def wait_for_node(node, env, timeout=20):
    # A fixed sleep before the first lifecycle call isn't reliable — a
    # fresh process importing torch can occasionally take a beat longer
    # than expected, and calling `ros2 lifecycle set` before the node has
    # registered in the ROS graph yet fails immediately with "Node not
    # Found" (seen on this exact sequence for /planner_server). Wait for
    # the node to actually exist first instead of guessing with a delay.
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = runcmd(f"ros2 node list --no-daemon 2>/dev/null | grep -qx '{node}'", env)
        if r.returncode == 0:
            return True
        time.sleep(0.5)
    return False

def wait_lifecycle_state(node, state, env, timeout=30):
    """Poll ros2 lifecycle get until node reports the expected state."""
    _state_num = {'unconfigured': '[1]', 'inactive': '[2]', 'active': '[3]'}
    target = _state_num.get(state, state)
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = runcmd(f"ros2 lifecycle get {node} --no-daemon 2>/dev/null", env)
        if target in r.stdout:
            return True
        time.sleep(1)
    return False

def lc_node(node, env, configure_timeout=30, activate_timeout=25, attempts=2):
    if not wait_for_node(node, env):
        log(f"{node} never appeared in 'ros2 node list' — process may have crashed on startup", "fail")
        return False
    for attempt in range(1, attempts + 1):
        tag = f"  (attempt {attempt}/{attempts})" if attempts > 1 else ""
        log(f"Lifecycle configure  {node}{tag}", "sys")
        runcmd(f"ros2 lifecycle set {node} configure --no-daemon 2>&1", env)
        if not wait_lifecycle_state(node, 'inactive', env, timeout=configure_timeout):
            log(f"Configure timeout for {node} – proceeding anyway", "warn")

        log(f"Lifecycle activate   {node}", "sys")
        runcmd(f"ros2 lifecycle set {node} activate --no-daemon 2>&1", env)
        if wait_lifecycle_state(node, 'active', env, timeout=activate_timeout):
            log(f"Active  {node}", "ok")
            return True

        log(f"{node} did not activate (attempt {attempt}/{attempts})", "warn")
        if attempt < attempts:
            log(f"Resetting {node} before retry...", "warn")
            runcmd(f"ros2 lifecycle set {node} deactivate --no-daemon 2>&1", env)
            runcmd(f"ros2 lifecycle set {node} cleanup --no-daemon 2>&1", env)
            time.sleep(2)

    log(f"FAILED to activate {node} after {attempts} attempts – check node logs", "fail")
    return False

def lc_ntfields(node, env, model_path=None, attempts=2):
    if model_path is not None and not Path(model_path).exists():
        msg = f"NTFields model not found: {model_path} – train it first"
        log(msg, "fail")
        report_progress("ERROR", msg)
        return False

    if not wait_for_node(node, env):
        msg = f"{node} never appeared in 'ros2 node list' — process may have crashed on startup"
        log(msg, "fail")
        report_progress("ERROR", msg)
        return False

    for attempt in range(1, attempts + 1):
        tag = f"  (attempt {attempt}/{attempts})" if attempts > 1 else ""
        log(f"Lifecycle configure  {node}  (loading NTFields model...){tag}", "sys")
        runcmd(f"ros2 lifecycle set {node} configure --no-daemon 2>&1", env)

        if not wait_lifecycle_state(node, 'inactive', env, timeout=50):
            log(f"Configure timed out for {node} – model may still be loading", "warn")
            time.sleep(5)

        log(f"Lifecycle activate   {node}", "sys")
        runcmd(f"ros2 lifecycle set {node} activate --no-daemon 2>&1", env)

        if wait_action("/compute_path_to_pose", env, timeout=45):
            log(f"Active  {node}  – /compute_path_to_pose ready", "ok")
            return True

        log(f"NTFields planner did not register action server (attempt {attempt}/{attempts})", "warn")
        if attempt < attempts:
            log(f"Resetting {node} before retry...", "warn")
            runcmd(f"ros2 lifecycle set {node} deactivate --no-daemon 2>&1", env)
            runcmd(f"ros2 lifecycle set {node} cleanup --no-daemon 2>&1", env)
            time.sleep(2)

    msg = f"NTFields planner ({node}) failed to activate after {attempts} attempts – check model path"
    log(msg, "fail")
    report_progress("ERROR", msg)
    return False

def step_done(name):
    global step_idx
    step_idx += 1
    # launch() sends every child's stdout AND stderr to DEVNULL, so a process
    # that dies on startup says nothing anywhere — not in the TUI, not in
    # /tmp/argo_nav_output.log — and this function would still report "Ready"
    # and tick the progress bar. That is exactly how a step launching an
    # executable that no longer existed ("No executable found") kept
    # reporting Ready, and the stack came up announcing "All Systems Nominal"
    # with that node missing. A process that has already exited by the time
    # its own step completes has failed, so say so: "fail" is the one log
    # level that reaches the UI through report_progress().
    p = pids.get(name)
    if p is not None and p.poll() is not None:
        log(f"FAILED  >>  {name} exited on startup (rc={p.returncode})", "fail")
        return
    log(f"Ready  >>  {name}", "ok")

# ──────────────────────────────────────────────────────────────────────────────
#  Cleanup
# ──────────────────────────────────────────────────────────────────────────────
def cleanup(sig=None, frame=None):
    save_last_pose(_last_env)   # while /pose is still alive — before anything below gets killed
    stop_ui.set()
    report_progress("STOPPED", "Stack shut down")
    log("Shutting down – terminating all nodes...", "warn")
    time.sleep(0.3)
    for _name, p in pids.items():
        try:
            os.killpg(os.getpgid(p.pid), signal.SIGTERM)
        except Exception:
            pass
    time.sleep(1)
    for _name, p in pids.items():
        try:
            os.killpg(os.getpgid(p.pid), signal.SIGKILL)
        except Exception:
            pass
    sys.stdout.write("\033[?25h\033[0m\n")
    sys.exit(0)

def ntfields_models_dir(home: str) -> Path:
    override = os.environ.get("NTFIELDS_MODELS_DIR")
    return Path(override) if override else Path(home) / "ntfields_models"

# ──────────────────────────────────────────────────────────────────────────────
#  Map selector
# ──────────────────────────────────────────────────────────────────────────────
def select_map(home: str, default: str) -> str:
    maps_dir   = Path(REPO_ROOT) / "src/argo_mini/maps"
    models_dir = ntfields_models_dir(home)

    entries = []
    seen    = set()
    for f in sorted(maps_dir.glob("*.yaml")):
        name = f.stem
        if name in seen:
            continue
        seen.add(name)
        has_model = (models_dir / f"{name}.pt").exists()
        entries.append((name, str(f.with_suffix("")), has_model))

    if not entries:
        print(f"{RED}No maps found in {maps_dir}{RS}")
        sys.exit(1)

    default_idx = next((i for i, (n, _, _) in enumerate(entries) if n == default), 0)

    W = 64
    print(f"\n{BORDER}+{'-'*(W-2)}+{RS}")
    title = f"  {GOLD}{BLD}  ARGO SONIC  –  Select Map  {RS}"
    pad   = " " * max(0, W - 2 - len("    ARGO SONIC  -  Select Map  "))
    print(f"{BORDER}|{RS}{BG_H}{title}{pad}{RS}{BORDER}|{RS}")
    print(f"{BORDER}+{'-'*(W-2)}+{RS}")

    for i, (name, _, has_model) in enumerate(entries):
        marker  = f"{GOLD}{BLD}>{RS}" if i == default_idx else " "
        num     = f"{CYAN}{i+1:>2}{RS}"
        status  = f"{GREEN}[model OK]{RS}" if has_model else f"{YELLOW}[no model]{RS}"
        col     = name[:36].ljust(36)
        content = f"  {marker} {num}  {WHITE}{col}{RS}  {status}"
        pad2    = " " * max(0, W - 2 - vlen(content))
        print(f"{BORDER}|{RS}{content}{pad2}{BORDER}|{RS}")

    print(f"{BORDER}+{'-'*(W-2)}+{RS}")
    print(f"  {DIM}Select [1-{len(entries)}]  default={default_idx+1} ({entries[default_idx][0]}) — auto-select in 5s: {RS}", end="", flush=True)

    try:
        ready, _, _ = select.select([sys.stdin], [], [], 5.0)
        if ready:
            raw = sys.stdin.readline().strip()
        else:
            print(f"\n{YELLOW}No input in 5s — using default.{RS}")
            raw = ""
    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit(0)

    if raw == "":
        chosen = entries[default_idx]
    else:
        try:
            n = int(raw)
            if not (1 <= n <= len(entries)):
                raise ValueError
            chosen = entries[n - 1]
        except ValueError:
            print(f"{YELLOW}Invalid – using default.{RS}")
            chosen = entries[default_idx]

    name, map_base, has_model = chosen
    if not has_model:
        print(f"\n  {YELLOW}Warning: no NTFields model for '{name}'.{RS}")
        print(f"  {GRAY}Run train_ntfields.py first. Planner will fail to load.{RS}\n")
        time.sleep(2)

    print(f"\n  {GREEN}Map:{RS}  {GOLD}{BLD}{name}{RS}  {GRAY}{map_base}{RS}\n")
    time.sleep(0.6)
    return map_base


# ──────────────────────────────────────────────────────────────────────────────
#  Main
# ──────────────────────────────────────────────────────────────────────────────
def main():
    global step_name

    parser = argparse.ArgumentParser(description="Argo Sonic NTFields Navigation Launcher")
    parser.add_argument("--no-cam", action="store_true", help="Skip depth camera")
    parser.add_argument("--no-rviz", action="store_true", help="Headless (run via the web UI) — no DISPLAY on the robot")
    parser.add_argument("--map", default=None, help="Map base path or name (no extension). Omit to get a selector.")
    args = parser.parse_args()

    home    = str(Path.home())
    no_cam  = args.no_cam
    no_rviz = args.no_rviz

    if args.map:
        raw = args.map.replace("~", home)
        if os.sep not in raw:
            raw = str(Path(REPO_ROOT) / "src/argo_mini/maps" / raw)
        map_base = str(Path(raw).with_suffix(""))
    elif sys.stdin.isatty():
        map_base = select_map(home, default="office_map2")
    else:
        print(f"{RED}--map is required when not running in an interactive terminal{RS}")
        sys.exit(1)

    signal.signal(signal.SIGINT,  cleanup)
    signal.signal(signal.SIGTERM, cleanup)

    sys.stdout.write("\033[2J\033[H\033[?25l")
    sys.stdout.flush()
    threading.Thread(target=ui_loop, daemon=True).start()

    # `ros2 daemon stop` talks to the daemon over its own RPC socket, so a
    # wedged daemon just eats the full 8s timeout below and never actually
    # dies. Killing it directly by process name first guarantees it's gone
    # before we ever try the graceful stop/start, instead of hoping the RPC
    # call works.
    log("Killing any stale ros2 daemon...", "sys")
    subprocess.run(["pkill", "-9", "-f", "_ros2_daemon"], capture_output=True)
    time.sleep(1)

    log("Clearing previous ROS processes...", "sys")
    for proc in [
        "slam_toolbox", "serial_bridge", "rplidar_composition", "rviz2",
        "ntfields_planner_node", "planner_server", "controller_server",
        "bt_navigator", "velocity_smoother", "scan_relay",
        "robot_state_publisher", "depth_safety_shield", "ekf_node",
        "ascamera_node", "pointcloud_restamper", "behavior_server", "safety_shield",
        "patrol_manager",
        # Both of these executables are gone, but a copy left running by an
        # older launch keeps its ROS node in the graph forever — this list is
        # the only thing that would ever reap it, and one mppi_reverse_controller
        # survived here for a full day because the name was never listed.
        # Deleting a node means adding it here, not just removing its launch.
        "mppi_reverse_controller", "goal_approach_limiter",
        # Same accumulation problem, different node: this script starts its own
        # camera TF bridge every run but never reaped the previous one, so
        # duplicates piled up across restarts all publishing the same static
        # transform. start_nav_stepwise.sh's own pkill list already includes
        # this name for the same reason.
        "static_transform_publisher",
    ]:
        subprocess.run(["pkill", "-9", "-f", proc], capture_output=True)
    time.sleep(3)

    # serial_bridge holds /dev/esp32 exclusively (pyserial) and is the only
    # publisher of /wheel_odom. If one survives the pkill above (started
    # manually, by another user, or just not yet reaped) the instance
    # launched later in this script fails to open the port and no odom data
    # ever appears — force-kill it here, verified, instead of assuming it
    # worked.
    for attempt in range(6):
        still_up = subprocess.run(["pgrep", "-f", "serial_bridge"], capture_output=True, text=True, timeout=5)
        if not still_up.stdout.strip():
            break
        subprocess.run(["pkill", "-9", "-f", "serial_bridge"], capture_output=True, timeout=5)
        time.sleep(0.5)

    # Force-free the port itself too, unconditionally — covers a holder
    # that doesn't show "serial_bridge" anywhere in its own command line,
    # which the name-based pkill above can't touch.
    try:
        subprocess.run(["fuser", "-k", "-9", "/dev/esp32"], capture_output=True, timeout=5)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    time.sleep(1)

    still_up = subprocess.run(["pgrep", "-f", "serial_bridge"], capture_output=True, text=True, timeout=5)
    if still_up.stdout.strip():
        log("A serial_bridge process survived the kill (likely started "
            "manually by another user, or holding /dev/esp32) — odom "
            "will probably fail below. Check `pgrep -af serial_bridge` / "
            "`fuser /dev/esp32` manually.", "warn")

    log("Sourcing ROS2 + argo_sonic workspace...", "sys")
    env = build_env(home)
    _last_env.update(env)   # so cleanup() (a signal handler with no access to this local) can still shell out correctly
    log("Environment ready", "ok")

    ws           = REPO_ROOT
    nav_cfg      = f"{ws}/install/argo_mini/share/argo_mini/config/nav2.yaml"
    slam_cfg     = f"{ws}/install/argo_mini/share/argo_mini/config/slam_toolbox.yaml"
    ntfields_cfg = f"{ws}/install/argo_mini/share/argo_mini/config/ntfields.yaml"
    sdk_ros      = f"{home}/EaiCameraSdk_v1.2.28.20241015/demo/linux_ros/ros2"

    subprocess.run(
        "chmod 666 /dev/esp32 /dev/lidar 2>/dev/null || "
        "sudo chmod 666 /dev/esp32 /dev/lidar 2>/dev/null || true",
        shell=True
    )

    # ── 1. Robot State Publisher ───────────────────────────────────────────────
    launch("Robot State Publisher",
           "ros2 launch argo_mini robot_state_publisher.launch.py", env)
    time.sleep(1); step_done("Robot State Publisher")

    # ── 2. Camera TF ──────────────────────────────────────────────────────────
    launch("Camera TF Bridge",
           ("ros2 run tf2_ros static_transform_publisher "
            "--x 0.2575 --y 0.0 --z 0.170 --roll 0.417281 --pitch -0.018276 --yaw 1.611996 "
            "--frame-id base_link --child-frame-id ascamera_hp60c_color_0"), env)
    time.sleep(0.5); step_done("Camera TF Bridge")

    # ── 3. Serial Bridge ──────────────────────────────────────────────────────
    launch_with_telem("Serial Bridge",
           ("ros2 run argo_mini serial_bridge --ros-args "
            "-p port:=/dev/esp32 -p baud:=115200 -p left_tick_scale:=0.66"), env)
    time.sleep(1); step_done("Serial Bridge")

    # ── 4. RPLidar ────────────────────────────────────────────────────────────
    # settle raised 6 -> 20: the readiness check itself (`ros2 topic list
    # --no-daemon`) is an ephemeral CLI call that must rediscover the whole
    # ROS graph from scratch every time — measured at 1.9s near-idle and
    # 3.3s+ under the load this stack actually runs under (Jetson, several
    # nodes already up). At 6s that left room for maybe one real check, so
    # an unlucky discovery-timing check could report "failed" even though
    # the lidar itself was already scanning fine (confirmed: it connects
    # and starts scanning in <2s every time run standalone).
    rplidar_ok = launch_with_retry("RPLidar A1",
           ("ros2 run rplidar_ros rplidar_composition --ros-args "
            "-p serial_port:=/dev/lidar -p serial_baudrate:=115200 "
            "-p frame_id:=lidar_link -p angle_compensate:=true -p scan_mode:=Boost"),
           env, ready_topic="/scan", attempts=3, settle=10)
    if rplidar_ok is None:
        report_progress("ERROR", "RPLidar failed to come up - no obstacle data for SLAM/safety_shield")
    step_done("RPLidar A1")

    # ── 5. Scan Relay ─────────────────────────────────────────────────────────
    launch("Scan Relay", "ros2 run argo_mini scan_relay", env)
    time.sleep(0.5); step_done("Scan Relay")

    # ── 6. SLAM Toolbox (localization) ────────────────────────────────────────
    launch("SLAM Toolbox",
           (f"ros2 run slam_toolbox localization_slam_toolbox_node --ros-args "
            f"--params-file {slam_cfg} -p map_file_name:={map_base}"), env)
    if not wait_topic("/map", env, timeout=40):
        log("SLAM map not published – check map file path", "warn")
    time.sleep(2); step_done("SLAM Toolbox")
    seed_initial_pose(env)   # from the last shutdown, if any — see save_last_pose() in cleanup()

    # Verify sensor flow & TFs before configuring downstream Nav2 servers
    wait_nav_prerequisites(env)

    # ── 7. NTFields Planner ───────────────────────────────────────────────────
    ntfields_model = str(ntfields_models_dir(home) / f"{Path(map_base).name}.pt")
    launch("NTFields Planner",
           (f"ros2 run argo_mini ntfields_planner_node --ros-args "
            f"--params-file {ntfields_cfg} -p model_path:={ntfields_model}"), env)
    time.sleep(6)
    ntfields_ok = lc_ntfields("/planner_server", env, model_path=ntfields_model)
    step_done("NTFields Planner")

    # ── 8. Controller Server ──────────────────────────────────────────────────
    launch("Controller Server",
           (f"ros2 run nav2_controller controller_server --ros-args "
            f"--params-file {nav_cfg} -r cmd_vel:=/cmd_vel_raw"), env)
    time.sleep(2)
    lc_node("/controller_server", env, configure_timeout=40, activate_timeout=30)
    step_done("Controller Server")

    # ── 9. Velocity Smoother ──────────────────────────────────────────────────
    launch("Velocity Smoother",
           (f"ros2 run nav2_velocity_smoother velocity_smoother --ros-args "
            f"--params-file {nav_cfg} "
            f"-r cmd_vel:=/cmd_vel_raw -r cmd_vel_smoothed:=/cmd_vel_smoothed"), env)
    time.sleep(1)
    lc_node("/velocity_smoother", env, configure_timeout=35, activate_timeout=25)
    step_done("Velocity Smoother")

    # ── 10. Behavior Server ───────────────────────────────────────────────────
    launch("Behavior Server",
           (f"ros2 run nav2_behaviors behavior_server --ros-args "
            f"--params-file {nav_cfg} -r cmd_vel:=/cmd_vel_raw"), env)
    time.sleep(1)
    lc_node("/behavior_server", env, configure_timeout=35, activate_timeout=25)
    step_done("Behavior Server")

    # ── Wait for all action servers ───────────────────────────────────────────
    log("Waiting for action servers...", "info")
    wait_action("/follow_path", env, 30)
    wait_action("/backup",      env, 30)
    time.sleep(3)

    # ── 11. BT Navigator ──────────────────────────────────────────────────────
    launch("BT Navigator",
           f"ros2 run nav2_bt_navigator bt_navigator --ros-args --params-file {nav_cfg} "
           f"-p default_nav_to_pose_bt_xml:={ws}/src/argo_mini/config/bt/navigate_to_pose.xml "
           f"-p default_nav_through_poses_bt_xml:={ws}/src/argo_mini/config/bt/navigate_to_pose.xml", env)
    time.sleep(5)
    bt_active = lc_node("/bt_navigator", env, configure_timeout=40, activate_timeout=30)
    step_done("BT Navigator")

    if not ntfields_ok:
        report_progress("ERROR", "NTFields planner failed to activate - path planning unavailable")
    elif bt_active:
        report_progress("READY", "Nav2 fully activated - ready for goals")
    else:
        report_progress("ERROR", "bt_navigator failed to activate - /navigate_to_pose is not available")

    # ── 12. Depth Camera ──────────────────────────────────────────────────────
    if not no_cam:
        launch("Depth Camera",
               (f"bash -c 'cd {sdk_ros} && source install/setup.bash && "
                f"ros2 launch ascamera hp60c.launch.py 2>&1'"), env)
        if not wait_topic("/ascamera_hp60c/camera_publisher/depth0/points", env, 15):
            log("Camera not publishing – check USB connection", "warn")
    else:
        log("Camera skipped  (--no-cam)", "warn")
    step_done("Depth Camera")

    # ── Verify smoother ───────────────────────────────────────────────────────
    if not wait_topic("/cmd_vel_smoothed", env, 10):
        log("Velocity smoother not publishing – aborting", "fail")
        cleanup()

    # ── 13. PointCloud Restamper ──────────────────────────────────────────────
    if not no_cam:
        launch("PC Restamper", "ros2 run argo_mini pointcloud_restamper", env)
        if not wait_topic("/ascamera_hp60c/camera_publisher/depth0/points_corrected", env, 10):
            log("points_corrected not publishing – voxel layer will have no depth data", "warn")
    step_done("PC Restamper")

    # ── 14. Safety Shield ─────────────────────────────────────────────────────
    launch("Safety Shield", "ros2 run argo_mini safety_shield", env)
    time.sleep(3); step_done("Safety Shield")

    # ── 14.5 Patrol Manager ─────────────────────────────────────────────────────
    # Runs the goal <-> home shuttle the dashboard's Patrol tool starts. The
    # loop lives in this node, not in the browser, so a refreshed or closed
    # tab cannot strand the robot mid-patrol.
    #
    # This slot used to launch mppi_reverse_controller, which has been deleted:
    # it tried to enable MPPI reverse during recovery, but its trigger
    # (last_clear_time) was only ever written by a hook nothing called, so it
    # never fired once. Reversing during recovery is now the BT's BackUp node,
    # through behavior_server. Nothing here noticed the executable had gone —
    # launch() discards child output and step_done() reported "Ready" anyway,
    # which is what the poll() check in step_done now catches.
    launch("Patrol Manager", "ros2 run argo_mini patrol_manager", env)
    time.sleep(1); step_done("Patrol Manager")

    # ── RViz (optional) ──────────────────────────────────────────────────────
    if not no_rviz:
        env["DISPLAY"] = ":1"
        launch("RViz", "rviz2", env)
    else:
        log("RViz skipped  (--no-rviz)", "warn")

    step_name = f"{GREEN}All Systems Nominal{RS}"
    log("----------------------------------------------------", "sys")
    log("NTFields navigation stack is LIVE", "ok")
    log(f"Map: {map_base}", "info")
    log(f"Planner: NTFields physics-informed (Eikonal)", "info")
    log(f"Camera: {'disabled' if no_cam else 'enabled'}", "info")
    log("Use RViz 2D Goal Pose to navigate  |  Ctrl+C to stop", "sys")
    log("----------------------------------------------------", "sys")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        cleanup()


if __name__ == "__main__":
    main()
