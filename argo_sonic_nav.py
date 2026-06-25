#!/usr/bin/env python3
"""
Argo Sonic – NTFields Navigation Launcher
Usage: python3 argo_sonic_nav.py [--no-cam] [--map /path/to/map]
"""

import os, sys, re, time, signal, shutil, subprocess, threading, argparse, io, math
from datetime import datetime
from pathlib import Path

WHEEL_RADIUS = 0.0762
WHEEL_BASE   = 0.41

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
    "Behavior Server",       "NTFields Planner",     "Controller Server",
    "Velocity Smoother",     "BT Navigator",         "Depth Camera",
    "Safety Shield",
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

def log(msg, kind="info"):
    ts    = datetime.now().strftime("%H:%M:%S")
    icon  = ICONS.get(kind, ICONS["info"])
    color = COLORS.get(kind, WHITE)
    line  = f"  {DIM}{GRAY}{ts}{RS}  {icon}  {color}{msg}{RS}"
    with log_lock:
        log_lines.append(line)

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
    ws  = f"{home}/argo_sonic"
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
    sdk = f"{home}/EaiCameraSdk_v1.2.28.20241015/demo/linux_ros/ros2"
    env["LD_LIBRARY_PATH"] = (
        env.get("LD_LIBRARY_PATH", "") +
        f":{sdk}/ascamera/libs/lib/aarch64-linux-gnu"
    )
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

def runcmd(cmd, env):
    return subprocess.run(cmd, shell=True, env=env, capture_output=True, text=True)

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

def lc_node(node, env):
    log(f"Lifecycle configure  {node}", "sys")
    runcmd(f"ros2 lifecycle set {node} configure 2>&1 | tail -1", env)
    time.sleep(2)
    log(f"Lifecycle activate   {node}", "sys")
    runcmd(f"ros2 lifecycle set {node} activate 2>&1 | tail -1", env)
    time.sleep(2)
    log(f"Active  {node}", "ok")

def wait_lifecycle_state(node, state, env, timeout=30):
    """Poll ros2 lifecycle get until node reports the expected state."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = runcmd(f"ros2 lifecycle get {node} 2>/dev/null", env)
        if state in r.stdout.lower():
            return True
        time.sleep(1)
    return False

def lc_ntfields(node, env):
    """
    Lifecycle sequence for NTFields planner.
    configure loads the model (~7s) which can exceed the CLI default timeout,
    so we poll the actual node state rather than trusting the CLI return value.
    """
    log(f"Lifecycle configure  {node}  (loading NTFields model...)", "sys")
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
    else:
        log(f"NTFields planner did not register action server – check model path", "warn")

def step_done(name):
    global step_idx
    step_idx += 1
    log(f"Ready  >>  {name}", "ok")

# ──────────────────────────────────────────────────────────────────────────────
#  Cleanup
# ──────────────────────────────────────────────────────────────────────────────
def cleanup(sig=None, frame=None):
    stop_ui.set()
    log("Shutting down – terminating all nodes...", "warn")
    time.sleep(0.3)
    for name, p in pids.items():
        try:
            os.killpg(os.getpgid(p.pid), signal.SIGTERM)
        except Exception:
            pass
    time.sleep(1)
    subprocess.run(["pkill", "-9", "-f", "ros2"], capture_output=True)
    sys.stdout.write("\033[?25h\033[0m\n")
    sys.exit(0)

# ──────────────────────────────────────────────────────────────────────────────
#  Main
# ──────────────────────────────────────────────────────────────────────────────
def main():
    global step_name

    parser = argparse.ArgumentParser(description="Argo Sonic NTFields Navigation Launcher")
    parser.add_argument("--no-cam", action="store_true", help="Skip depth camera")
    parser.add_argument("--map",
                        default="~/argo_sonic/src/argo_mini/maps/office_map",
                        help="Map path (no extension)")
    args = parser.parse_args()

    home     = str(Path.home())
    map_base = args.map.replace("~", home)
    no_cam   = args.no_cam

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
        "ascamera_node", "behavior_server",
    ]:
        subprocess.run(["pkill", "-9", "-f", proc], capture_output=True)
    time.sleep(3)

    # Build ROS env
    log("Sourcing ROS2 + argo_sonic workspace...", "sys")
    env = build_env(home)
    log("Environment ready", "ok")

    ws           = f"{home}/argo_sonic"
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
            "-p port:=/dev/ttyUSB1 -p baud:=115200 -p left_tick_scale:=2.0"), env)
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
    time.sleep(5); step_done("SLAM Toolbox")

    # ── 7. Behavior Server ────────────────────────────────────────────────────
    launch("Behavior Server",
           (f"ros2 run nav2_behaviors behavior_server --ros-args "
            f"--params-file {nav_cfg} -r cmd_vel:=/cmd_vel_raw"), env)
    time.sleep(5)
    wait_topic("/local_costmap/costmap_raw", env, 15)
    wait_topic("/global_costmap/costmap_raw", env, 15)
    lc_node("/behavior_server", env)
    step_done("Behavior Server")

    # ── 8. NTFields Planner ───────────────────────────────────────────────────
    launch("NTFields Planner",
           (f"ros2 run argo_mini ntfields_planner_node --ros-args "
            f"--params-file {ntfields_cfg}"), env)
    time.sleep(6)   # Python node + torch import needs extra startup time
    lc_ntfields("/planner_server", env)
    step_done("NTFields Planner")

    # ── 9. Controller Server ──────────────────────────────────────────────────
    launch("Controller Server",
           (f"ros2 run nav2_controller controller_server --ros-args "
            f"--params-file {nav_cfg} -r cmd_vel:=/cmd_vel_raw"), env)
    time.sleep(3); lc_node("/controller_server", env); step_done("Controller Server")

    # ── 10. Velocity Smoother ─────────────────────────────────────────────────
    launch("Velocity Smoother",
           (f"ros2 run nav2_velocity_smoother velocity_smoother --ros-args "
            f"--params-file {nav_cfg} "
            f"-r cmd_vel:=/cmd_vel_raw -r cmd_vel_smoothed:=/cmd_vel_smoothed"), env)
    time.sleep(3); lc_node("/velocity_smoother", env); step_done("Velocity Smoother")

    # ── Wait for all action servers ───────────────────────────────────────────
    log("Waiting for action servers...", "info")
    wait_action("/follow_path", env, 30)
    wait_action("/backup",      env, 30)
    time.sleep(3)

    # ── 11. BT Navigator ──────────────────────────────────────────────────────
    launch("BT Navigator",
           f"ros2 run nav2_bt_navigator bt_navigator --ros-args --params-file {nav_cfg}", env)
    time.sleep(5); lc_node("/bt_navigator", env); step_done("BT Navigator")

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

    # ── 13. Safety Shield ─────────────────────────────────────────────────────
    launch("Safety Shield",
           ("ros2 run argo_mini depth_safety_shield --ros-args "
            "-p stop_distance:=0.85 -p tunnel_width:=0.25 -p min_points:=70 "
            "-p height_min:=0.20 -p height_max:=1.80 "
            "-p input_topic:=/cmd_vel_smoothed -p output_topic:=/cmd_vel "
            "-p depth_topic:=/ascamera_hp60c/camera_publisher/depth0/points"), env)
    time.sleep(5); step_done("Safety Shield")

    # ── RViz ──────────────────────────────────────────────────────────────────
    env["DISPLAY"] = ":1"
    launch("RViz", "rviz2", env)

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
