#!/usr/bin/env python3
"""
start_slam_explore_autosave.py – Autonomous SLAM exploration with automatic
map saving once exploration completes.

Prompts for a map name, launches start_slam_explore.sh unmodified (reusing
the already-working exploration stack as-is), waits for frontier_explorer to
report /exploration/complete (published after NO_FRONTIER_DEBOUNCE consecutive
ticks with no frontiers left — see frontier_explorer.py), then saves the map
in all four formats and shuts the stack down.

Usage:
    python3 start_slam_explore_autosave.py
"""

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import rclpy
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, HistoryPolicy, QoSProfile, ReliabilityPolicy
from std_msgs.msg import Bool

SCRIPT_DIR   = Path(__file__).resolve().parent
EXPLORE_SH   = SCRIPT_DIR / "start_slam_explore.sh"
MAPS_DIR     = Path.home() / "argo_sonic/src/argo_mini/maps"
SAVE_TIMEOUT = 30   # s – per service call


class CompletionWatcher(Node):

    def __init__(self):
        super().__init__("explore_autosave_watcher")
        self.complete = False
        qos = QoSProfile(
            reliability=ReliabilityPolicy.RELIABLE,
            history=HistoryPolicy.KEEP_LAST,
            depth=1,
            durability=DurabilityPolicy.TRANSIENT_LOCAL,
        )
        self.create_subscription(Bool, "/exploration/complete", self._on_complete, qos)

    def _on_complete(self, msg: Bool) -> None:
        if msg.data:
            self.complete = True


def save_map(name: str) -> bool:
    out_path = str(MAPS_DIR / name)
    MAPS_DIR.mkdir(parents=True, exist_ok=True)

    print(f"[autosave] Saving map as '{name}' -> {out_path}.{{yaml,pgm,posegraph,data}}")

    print("[autosave] Calling /slam_toolbox/save_map (yaml + pgm)...")
    r1 = subprocess.run(
        ["ros2", "service", "call", "/slam_toolbox/save_map",
         "slam_toolbox/srv/SaveMap", f"{{name: {{data: '{out_path}'}}}}"],
        capture_output=True, text=True, timeout=SAVE_TIMEOUT,
    )
    print(r1.stdout.strip())
    if r1.stderr.strip():
        print(r1.stderr.strip())

    print("[autosave] Calling /slam_toolbox/serialize_map (posegraph + data)...")
    r2 = subprocess.run(
        ["ros2", "service", "call", "/slam_toolbox/serialize_map",
         "slam_toolbox/srv/SerializePoseGraph", f"{{filename: '{out_path}'}}"],
        capture_output=True, text=True, timeout=SAVE_TIMEOUT,
    )
    print(r2.stdout.strip())
    if r2.stderr.strip():
        print(r2.stderr.strip())

    # Ground truth: did the files actually land on disk, not just "did the
    # service call return 0" (a service can accept the call and still fail
    # to write, e.g. bad path / no permissions).
    expected = [f"{name}.yaml", f"{name}.pgm", f"{name}.posegraph", f"{name}.data"]
    missing  = [f for f in expected if not (MAPS_DIR / f).exists()]

    if missing:
        print(f"[autosave] FAILED – missing files: {missing}")
        return False

    print(f"[autosave] All 4 files confirmed present in {MAPS_DIR}")
    return True


def shutdown_stack(proc: subprocess.Popen) -> None:
    print("[autosave] Shutting down exploration stack...")
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except Exception:
        pass
    time.sleep(5)
    subprocess.run(["pkill", "-9", "-f", "ros2"], capture_output=True)


_stop_requested = False


def _handle_sigint(signum, frame) -> None:
    global _stop_requested
    _stop_requested = True


def main() -> None:
    map_name = input("Map name to save as (no extension): ").strip()
    if not map_name:
        print("No name given, aborting.")
        sys.exit(1)

    if not EXPLORE_SH.exists():
        print(f"[autosave] {EXPLORE_SH} not found, aborting.")
        sys.exit(1)

    print(f"[autosave] Launching {EXPLORE_SH.name} ...")
    proc = subprocess.Popen(
        ["bash", str(EXPLORE_SH)],
        cwd=str(SCRIPT_DIR),
        preexec_fn=os.setsid,
    )

    rclpy.init()
    # rclpy.init() installs its own SIGINT handler, which can swallow Ctrl+C
    # before a `except KeyboardInterrupt` block ever sees it. Re-registering
    # our own handler afterward reclaims it, using a plain flag instead of
    # relying on exception propagation through rclpy's spin call.
    signal.signal(signal.SIGINT, _handle_sigint)
    watcher = CompletionWatcher()

    interrupted = False
    exited_early = False
    ok = False

    try:
        print("[autosave] Exploring — waiting for EXPLORATION COMPLETE "
              "(sustained 'no frontiers' from frontier_explorer)... "
              "Ctrl+C to stop early.")
        while rclpy.ok() and not watcher.complete:
            if _stop_requested:
                interrupted = True
                break
            rclpy.spin_once(watcher, timeout_sec=1.0)
            if proc.poll() is not None:
                print("[autosave] start_slam_explore.sh exited early — aborting.")
                exited_early = True
                break

        if interrupted:
            print("\n[autosave] Interrupted — shutting down without saving.")
        elif not exited_early:
            print("[autosave] EXPLORATION COMPLETE received.")
            ok = save_map(map_name)
    finally:
        # Always runs — Ctrl+C, normal completion, early exit, or any
        # unexpected error all end up here so the stack never gets orphaned.
        watcher.destroy_node()
        rclpy.shutdown()
        shutdown_stack(proc)

    if interrupted or exited_early:
        sys.exit(0 if interrupted else 1)
    elif ok:
        print(f"[autosave] Done. Map '{map_name}' saved in {MAPS_DIR}")
    else:
        print("[autosave] Done, but saving failed — check output above. "
              "Stack has been shut down; you may need to re-run exploration.")
        sys.exit(1)


if __name__ == "__main__":
    main()
