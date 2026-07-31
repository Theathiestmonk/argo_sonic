#!/usr/bin/env python3
"""
Argo Sonic – NTFields Navigation Launcher
Usage: python3 argo_sonic_nav.py [--no-cam] [--map /path/to/map]
"""

import os, sys, re, time, signal, shutil, subprocess, threading, argparse, io, math, select
from datetime import datetime
from pathlib import Path

WHEEL_RADIUS = 0.0762
WHEEL_BASE   = 0.41

# Repo root, derived from this file's own location — not hardcoded to
# ~/argo_sonic, since this checkout can (and on the actual robot, does)
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
log_lines  : list = []
log_lock   = threading.Lock()
ui_lock    = threading.Lock()
step_idx   = 0
step_name  = "Initializing..."
spin_frame = 0
stop_ui    = threading.Event()

telem      = {"lin": 0.0, "ang": 0.0, "rpm_l": 0.0, "rpm_r": 0.0}
telem_lock = threading.Lock()

TOTAL_STEPS = 13

STEP_NAMES = [
    "Robot State Publisher", "Camera TF Bridge",    "Serial Bridge",
    "RPLidar A1",            "Scan Relay",           "SLAM Toolbox",
    "NTFields Planner",      "Controller Server",    "Velocity Smoother",
    "Behavior Server",       "BT Navigator",         "Depth Camera",
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

# backend/launcher.py discards this script's stdout/stderr into a plain log
# file (see GET /nav_log) — fine for a human tailing it, but the live TUI
# above is full-screen ANSI redraws, not a clean step trail. Write the
# current step here too, same "status|epoch|message" format
# sh/start_argo_nav_ui.sh's report()/report_error()/report_ready() use, so
# the Dashboard's live progress button keeps working no matter which nav
# script is actually running.
PROGRESS_FILE = "/tmp/argo_nav_progress"
# Once True, routine "run"-kind progress notes stop overwriting the file —
# only report_progress("ERROR", ...) or a fresh explicit call can move past
# it. Without this, ANY real failure (a lifecycle node never reaching
# "active", not just BT Navigator specifically) would be silently erased
# the moment the next step's ordinary "Starting X..." message came in,
# since this script never aborts on a failed activation — it just logs a
# warning/fail and keeps going. Set on both "READY" (so later benign steps
# like the camera/safety shield don't erase it — this exact bug already
# happened once on sh/start_argo_nav_ui.sh) and "ERROR" (so a genuine
# failure sticks and is actually visible instead of getting overwritten by
# the very next routine step).
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
    # sh/start-rosbridge.sh forces Cyclone DDS (Fast-DDS, ROS2 Humble's
    # default, proved less reliable under discovery/service-call load on
    # this hardware — see that script's own comment for the story). This
    # script never set it at all, so every node it launches was running on
    # a DIFFERENT RMW than rosbridge — two different RMW implementations
    # can't discover each other's nodes at all, not just unreliably, which
    # would explain both the UI never seeing the robot as ready AND a
    # separate ROS2 action client (waypoint_manager.py) timing out too.
    r = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True)
    env = {}
    for line in r.stdout.splitlines():
        k, _, v = line.partition("=")
        if k:
            env[k] = v
    # A failed `source` (e.g. {ws}/install/setup.bash doesn't exist because
    # this checkout was never colcon-built) makes the whole `&&` chain
    # short-circuit before reaching `env`, so r.stdout is empty and this
    # dict ends up missing PATH entirely — which used to surface many steps
    # later as a bare "FileNotFoundError: 'ros2'" traceback from an
    # unrelated-looking line, instead of pointing at the actual cause here.
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
#  Launchers
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

def runcmd(cmd, env, timeout=10):
    """subprocess.run with a hard timeout. The outer polling loops
    (wait_lifecycle_state, wait_topic, wait_action) all have their own
    deadlines, but the individual command that actually SENDS a lifecycle
    transition (e.g. `ros2 lifecycle set /controller_server configure`)
    previously had none — if that single call hung (e.g. a DDS
    discovery/service-call issue, the same class of problem found with
    rosapi/rosbridge earlier), the whole script blocked on that one line
    forever, with nothing able to recover from it. A timed-out call is
    treated as a plain failure (nonzero returncode, empty output) so every
    existing caller's `r.returncode == 0` / `target in r.stdout` checks
    keep working unchanged."""
    try:
        return subprocess.run(cmd, shell=True, env=env, capture_output=True,
                               text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(cmd, returncode=124, stdout="", stderr="timed out")

def wait_topic(topic, env, timeout=30):
    log(f"Waiting for topic  {topic}", "info")
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = runcmd(f"ros2 topic list 2>/dev/null | grep -qx '{topic}'", env)
        if r.returncode == 0:
            log(f"Topic ready  {topic}", "ok")
            return True
        time.sleep(1)
    log(f"Timeout – topic not found: {topic}", "warn")
    return False

def wait_action(action, env, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = runcmd(f"ros2 action list 2>/dev/null | grep -qx '{action}'", env)
        if r.returncode == 0:
            return True
        time.sleep(1)
    log(f"Timeout – action not found: {action}", "warn")
    return False

def wait_lifecycle_active(node, env, timeout=30):
    """Poll until the lifecycle node reports 'active' state."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = runcmd(f"ros2 lifecycle get {node} 2>/dev/null", env)
        if 'active' in r.stdout.lower():
            return True
        time.sleep(1)
    log(f"Timeout – {node} never reached active state", "warn")
    return False

def lc_node(node, env, configure_timeout=30, activate_timeout=25, attempts=2):
    """Returns True only if the node actually confirmed 'active' — callers
    that unconditionally proceed (e.g. reporting READY) after calling this
    without checking the return value were treating a real activation
    failure as a plain warning and claiming ready regardless.

    Retries the full configure→activate cycle, same pattern lc_ntfields()
    already uses for /planner_server. A cold Jetson start (cold page cache,
    CPU governor not yet ramped) routinely makes the first configure call
    lose the race against the node's own startup — it lands while the node
    is still coming up, so the transition silently no-ops and the node is
    stuck 'unconfigured' for both the configure AND activate timeouts. That's
    exactly the "controller_server/velocity_smoother/bt_navigator failed on
    the first run, worked on the second" symptom — the second run only
    "worked" because everything was already warm by then. deactivate+cleanup
    between attempts are harmless no-ops if the node never even reached
    'inactive' — what actually matters is giving `configure` a second try a
    couple seconds later, after the race has had time to resolve.
    """
    for attempt in range(1, attempts + 1):
        tag = f"  (attempt {attempt}/{attempts})" if attempts > 1 else ""
        log(f"Lifecycle configure  {node}{tag}", "sys")
        runcmd(f"ros2 lifecycle set {node} configure 2>&1", env)
        if not wait_lifecycle_state(node, 'inactive', env, timeout=configure_timeout):
            log(f"Configure timeout for {node} – proceeding anyway", "warn")

        log(f"Lifecycle activate   {node}", "sys")
        runcmd(f"ros2 lifecycle set {node} activate 2>&1", env)
        if wait_lifecycle_state(node, 'active', env, timeout=activate_timeout):
            log(f"Active  {node}", "ok")
            return True

        log(f"{node} did not activate (attempt {attempt}/{attempts})", "warn")
        if attempt < attempts:
            log(f"Resetting {node} before retry...", "warn")
            runcmd(f"ros2 lifecycle set {node} deactivate 2>&1", env)
            runcmd(f"ros2 lifecycle set {node} cleanup 2>&1", env)
            time.sleep(2)

    log(f"FAILED to activate {node} after {attempts} attempts – check node logs", "fail")
    return False

def wait_lifecycle_state(node, state, env, timeout=30):
    """Poll ros2 lifecycle get until node reports the expected state."""
    # Use state numbers to avoid substring matches ('active' in 'inactive')
    _state_num = {'unconfigured': '[1]', 'inactive': '[2]', 'active': '[3]'}
    target = _state_num.get(state, state)
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = runcmd(f"ros2 lifecycle get {node} 2>/dev/null", env)
        if target in r.stdout:
            return True
        time.sleep(1)
    return False

def lc_ntfields(node, env, model_path=None, attempts=2):
    """
    Lifecycle sequence for NTFields planner.
    configure loads the model (~7s, longer under GPU/thermal load) which can
    exceed the CLI default timeout, so we poll the actual node state rather
    than trusting the CLI return value. Returns True only if
    /compute_path_to_pose actually registered — a single failed pass used to
    just log a warning and let the rest of the stack come up anyway, so
    "All Systems Nominal" could show with a completely non-functional
    planner. Retries the full configure→activate cycle (with a
    deactivate+cleanup reset between attempts) instead of giving up after
    one pass, and fails fast with a clear reported error if the model file
    for this map was never trained in the first place — no point spending
    ~50s waiting on a lifecycle sequence that's guaranteed to fail.
    """
    if model_path is not None and not Path(model_path).exists():
        msg = f"NTFields model not found: {model_path} – train it first"
        log(msg, "fail")
        report_progress("ERROR", msg)
        return False

    for attempt in range(1, attempts + 1):
        tag = f"  (attempt {attempt}/{attempts})" if attempts > 1 else ""
        log(f"Lifecycle configure  {node}  (loading NTFields model...){tag}", "sys")
        runcmd(f"ros2 lifecycle set {node} configure 2>&1", env)

        # Wait until node reports 'inactive' (configure done), up to 30s
        if not wait_lifecycle_state(node, 'inactive', env, timeout=30):
            log(f"Configure timed out for {node} – model may still be loading", "warn")
            # Give it extra time before trying activate anyway
            time.sleep(5)

        log(f"Lifecycle activate   {node}", "sys")
        runcmd(f"ros2 lifecycle set {node} activate 2>&1", env)

        if wait_action("/compute_path_to_pose", env, timeout=15):
            log(f"Active  {node}  – /compute_path_to_pose ready", "ok")
            return True

        log(f"NTFields planner did not register action server (attempt {attempt}/{attempts})", "warn")
        if attempt < attempts:
            log(f"Resetting {node} before retry...", "warn")
            runcmd(f"ros2 lifecycle set {node} deactivate 2>&1", env)
            runcmd(f"ros2 lifecycle set {node} cleanup 2>&1", env)
            time.sleep(2)

    msg = f"NTFields planner ({node}) failed to activate after {attempts} attempts – check model path"
    log(msg, "fail")
    report_progress("ERROR", msg)
    return False

def step_done(name):
    global step_idx
    step_idx += 1
    log(f"Ready  >>  {name}", "ok")

# ──────────────────────────────────────────────────────────────────────────────
#  Cleanup
# ──────────────────────────────────────────────────────────────────────────────
def cleanup(sig=None, frame=None):
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
    # Was "pkill -9 -f ros2" here — matches ANY process with ros2 anywhere
    # in its command line, system-wide. On this same robot that collaterally
    # killed the independent argo-rosbridge systemd service (it also runs
    # via "ros2 launch") every time a nav stack stopped — confirmed and
    # fixed once already in sh/start_argo_nav_ui.sh. Each child here got its
    # own session via preexec_fn=os.setsid, so there's no single shared
    # process group to sweep the way that script's trap does; instead,
    # SIGKILL specifically the children this script itself started, by
    # their own pgid, as the "still alive after SIGTERM" fallback.
    for _name, p in pids.items():
        try:
            os.killpg(os.getpgid(p.pid), signal.SIGKILL)
        except Exception:
            pass
    sys.stdout.write("\033[?25h\033[0m\n")
    sys.exit(0)

def ntfields_models_dir(home: str) -> Path:
    """Where trained NTFields .pt models live. Path.home() alone is fragile
    here — it reflects whichever user/UID actually runs this process, which
    doesn't always match wherever the models were actually trained/copied to
    (confirmed on argo-desktop: the systemd service ran as root, so
    Path.home() was /root, while the real models sit under
    /home/argo/ntfields_models, owned by argo — a "model not found" failure
    that had nothing to do with the model actually being missing). Setting
    NTFIELDS_MODELS_DIR overrides this outright, independent of which user
    ends up running the process."""
    override = os.environ.get("NTFIELDS_MODELS_DIR")
    return Path(override) if override else Path(home) / "ntfields_models"

# ──────────────────────────────────────────────────────────────────────────────
#  Map selector  (runs before TUI – normal terminal, print/input work fine)
# ──────────────────────────────────────────────────────────────────────────────
def select_map(home: str, default: str) -> str:
    """
    Print a numbered list of available maps and return the chosen map_base path.
    Called only when --map is not supplied on the command line.
    """
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

    # input() has no native timeout — select.select() on stdin waits for
    # input with a deadline instead of blocking forever, so a run left
    # unattended at this prompt falls through to the default map after 5s
    # instead of hanging indefinitely. (Validated separately on
    # argo_sonic_nav1.py before porting here.)
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
    parser.add_argument("--map",
                        default=None,
                        help="Map base path or name (no extension). Omit to get a selector.")
    args = parser.parse_args()

    home    = str(Path.home())
    no_cam  = args.no_cam
    no_rviz = args.no_rviz

    if args.map:
        raw = args.map.replace("~", home)
        # bare name (no path separator) → resolve into maps directory
        if os.sep not in raw:
            raw = str(Path(REPO_ROOT) / "src/argo_mini/maps" / raw)
        map_base = str(Path(raw).with_suffix(""))
    elif sys.stdin.isatty():
        map_base = select_map(home, default="office_map")
    else:
        # No --map and no terminal to prompt on (e.g. launched headlessly by
        # backend/launcher.py) — select_map()'s input() would hang forever
        # waiting for stdin that will never arrive, identical to the "no TTY"
        # issue DEPLOYMENT.md already documents for sonic/'s voice session
        # subprocess. Fail fast with a clear reason instead.
        print(f"{RED}--map is required when not running in an interactive terminal{RS}")
        sys.exit(1)

    signal.signal(signal.SIGINT,  cleanup)
    signal.signal(signal.SIGTERM, cleanup)

    sys.stdout.write("\033[2J\033[H\033[?25l")
    sys.stdout.flush()
    threading.Thread(target=ui_loop, daemon=True).start()

    # Kill stale processes
    log("Clearing previous ROS processes...", "sys")
    for proc in [
        "slam_toolbox", "serial_bridge", "rplidar_composition", "rviz2",
        "ntfields_planner_node", "planner_server", "controller_server",
        "bt_navigator", "velocity_smoother", "scan_relay",
        "robot_state_publisher", "depth_safety_shield",
        "ascamera_node", "pointcloud_restamper", "behavior_server", "safety_shield",
    ]:
        subprocess.run(["pkill", "-9", "-f", proc], capture_output=True)
    time.sleep(3)

    # Build ROS env
    log("Sourcing ROS2 + argo_sonic workspace...", "sys")
    env = build_env(home)
    log("Environment ready", "ok")

    # ros2 lifecycle set/get (used throughout below by lc_node) goes through
    # a shared background daemon for discovery caching. A stale daemon
    # makes EVERY lifecycle transition fail uniformly — not just one node —
    # which is exactly the "controller_server, velocity_smoother,
    # behavior_server, AND bt_navigator all failed" symptom this fixes.
    # Already confirmed as the root cause once on sh/start_argo_nav_ui.sh;
    # this script never had the same reset, so it was exposed to the same
    # bug the whole time.
    log("Resetting ros2 daemon...", "sys")
    subprocess.run(["ros2", "daemon", "stop"], env=env, capture_output=True)
    subprocess.run(["ros2", "daemon", "start"], env=env, capture_output=True)
    time.sleep(2)

    ws           = REPO_ROOT
    nav_cfg      = f"{ws}/install/argo_mini/share/argo_mini/config/nav2.yaml"
    slam_cfg     = f"{ws}/install/argo_mini/share/argo_mini/config/slam_toolbox.yaml"
    ntfields_cfg = f"{ws}/install/argo_mini/share/argo_mini/config/ntfields.yaml"
    sdk_ros      = f"{home}/EaiCameraSdk_v1.2.28.20241015/demo/linux_ros/ros2"

    subprocess.run(
        "chmod 666 /dev/ttyUSB0 /dev/ttyUSB1 2>/dev/null || "
        "sudo chmod 666 /dev/ttyUSB0 /dev/ttyUSB1 2>/dev/null || true",
        shell=True
    )

    # ── 1. Robot State Publisher ───────────────────────────────────────────────
    launch("Robot State Publisher",
           "ros2 launch argo_mini robot_state_publisher.launch.py", env)
    time.sleep(3); step_done("Robot State Publisher")

    # ── 2. Camera TF ──────────────────────────────────────────────────────────
    launch("Camera TF Bridge",
           ("ros2 run tf2_ros static_transform_publisher "
            "--x 0.2575 --y 0.0 --z 0.170 --roll 0.0 --pitch 0.0 --yaw 0.0 "
            "--frame-id base_link --child-frame-id ascamera_hp60c_color_0"), env)
    time.sleep(2); step_done("Camera TF Bridge")

    # ── 3. Serial Bridge ──────────────────────────────────────────────────────
    launch_with_telem("Serial Bridge",
           ("ros2 run argo_mini serial_bridge --ros-args "
            "-p port:=/dev/ttyUSB1 -p baud:=115200 -p left_tick_scale:=0.66"), env)
    time.sleep(3); step_done("Serial Bridge")

    # ── 4. RPLidar ────────────────────────────────────────────────────────────
    launch("RPLidar A1",
           ("ros2 run rplidar_ros rplidar_composition --ros-args "
            "-p serial_port:=/dev/ttyUSB0 -p serial_baudrate:=115200 "
            "-p frame_id:=lidar_link -p angle_compensate:=true -p scan_mode:=Boost"), env)
    time.sleep(3); step_done("RPLidar A1")

    # ── 5. Scan Relay ─────────────────────────────────────────────────────────
    launch("Scan Relay", "ros2 run argo_mini scan_relay", env)
    time.sleep(2); step_done("Scan Relay")

    # ── 6. SLAM Toolbox (localization) ────────────────────────────────────────
    launch("SLAM Toolbox",
           (f"ros2 run slam_toolbox localization_slam_toolbox_node --ros-args "
            f"--params-file {slam_cfg} -p map_file_name:={map_base}"), env)
    # Wait for SLAM to publish /map before Nav2 nodes configure (needs TF)
    if not wait_topic("/map", env, timeout=30):
        log("SLAM map not published – check map file path", "warn")
    time.sleep(3); step_done("SLAM Toolbox")

    # ── 7. NTFields Planner ───────────────────────────────────────────────────
    launch("NTFields Planner",
           (f"ros2 run argo_mini ntfields_planner_node --ros-args "
            f"--params-file {ntfields_cfg}"), env)
    time.sleep(6)   # Python node + torch import needs extra startup time
    ntfields_model = str(ntfields_models_dir(home) / f"{Path(map_base).name}.pt")
    ntfields_ok = lc_ntfields("/planner_server", env, model_path=ntfields_model)
    step_done("NTFields Planner")

    # ── 8. Controller Server ──────────────────────────────────────────────────
    launch("Controller Server",
           (f"ros2 run nav2_controller controller_server --ros-args "
            f"--params-file {nav_cfg} -r cmd_vel:=/cmd_vel_raw"), env)
    time.sleep(3); lc_node("/controller_server", env); step_done("Controller Server")

    # ── 9. Velocity Smoother ──────────────────────────────────────────────────
    launch("Velocity Smoother",
           (f"ros2 run nav2_velocity_smoother velocity_smoother --ros-args "
            f"--params-file {nav_cfg} "
            f"-r cmd_vel:=/cmd_vel_raw -r cmd_vel_smoothed:=/cmd_vel_smoothed"), env)
    time.sleep(3); lc_node("/velocity_smoother", env); step_done("Velocity Smoother")

    # ── 10. Behavior Server ───────────────────────────────────────────────────
    # Launched after controller_server so local costmap TF and /map are available
    launch("Behavior Server",
           (f"ros2 run nav2_behaviors behavior_server --ros-args "
            f"--params-file {nav_cfg} -r cmd_vel:=/cmd_vel_raw"), env)
    time.sleep(3)
    lc_node("/behavior_server", env)
    step_done("Behavior Server")

    # ── Wait for all action servers ───────────────────────────────────────────
    log("Waiting for action servers...", "info")
    wait_action("/follow_path", env, 30)
    wait_action("/backup",      env, 30)
    time.sleep(3)

    # ── 11. BT Navigator ──────────────────────────────────────────────────────
    launch("BT Navigator",
           f"ros2 run nav2_bt_navigator bt_navigator --ros-args --params-file {nav_cfg}", env)
    time.sleep(5)
    bt_active = lc_node("/bt_navigator", env)
    step_done("BT Navigator")
    # Gate READY on BOTH bt_navigator and the NTFields planner — bt_navigator
    # activating fine says nothing about whether /planner_server can
    # actually compute a path. ntfields_ok already reported its own ERROR
    # (and locked progress) the moment it failed, several steps back; check
    # it first here so that stays the reported reason instead of getting
    # overwritten by a "bt_navigator failed" message for a problem that
    # was really the planner's.
    if not ntfields_ok:
        report_progress("ERROR", "NTFields planner failed to activate - path planning unavailable")
    elif bt_active:
        report_progress("READY", "Nav2 fully activated - ready for goals")
    else:
        # This used to report READY unconditionally right here, regardless
        # of whether bt_navigator's lifecycle activation actually
        # succeeded — meaning a genuine activation failure looked
        # identical to success everywhere except this one easy-to-miss
        # "fail"-kind log line, and the UI would show "ready" while
        # /navigate_to_pose was never actually up.
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
