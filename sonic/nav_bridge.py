"""
nav_bridge.py — one-shot bridge between sonic_agent.py (a plain, non-ROS
Python process) and Nav2's /navigate_to_pose action.

sonic_agent.py itself deliberately stays out of ROS (runs in its own venv,
no rclpy) — this script is the one small piece that needs the sourced ROS
environment, invoked as a subprocess per navigation leg, matching how
backend/launcher.py's /estop handler shells out to `ros2` from a non-ROS
process. It exists instead of parsing `ros2 action send_goal`'s
human-readable CLI output because that format isn't meant to be parsed
reliably — this gets a real rclpy ActionClient and checks the actual
GoalStatus enum.

Goal construction mirrors src/argo_mini/argo_mini/waypoint_manager.py's
go_to() exactly (frame_id="map", position x/y, orientation z=qz/w=qw only)
— the one proven-working NavigateToPose goal shape in this repo.

Usage:
    python3 nav_bridge.py --map office_map --destination "Table 3" --timeout 60

Must be run with the ROS environment sourced first (rclpy/nav2_msgs need to
be importable) — sonic_agent.py's navigate_and_wait() invokes it that way.

Prints exactly one final line and sets the process exit code accordingly,
so the caller's check is a single string/returncode comparison:
    RESULT:SUCCESS   (exit 0)
    RESULT:FAILED    (exit 1)  — goal rejected or Nav2 reported failure
    RESULT:TIMEOUT   (exit 1)  — no result within --timeout
"""

import argparse
import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

WAYPOINTS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src", "argo_mini", "waypoints"
)

# "Robot starting to move" cue — same device-detection/volume pattern as
# safety_shield.py's own alert clip (the Jetson's USB audio card isn't the
# ALSA default, so mpg123 needs pointing at it explicitly there).
_ON_JETSON      = os.path.exists("/etc/nv_tegra_release") or "argo" in socket.gethostname().lower()
SPEAKER_DEVICE  = "plughw:CARD=Device,DEV=0" if _ON_JETSON else "default"
START_SOUND_FILE = str(Path(__file__).resolve().parent.parent / "sound" / "robot_start_sound.mp3")
START_SOUND_VOLUME = 0.85   # 0.0-1.0 — matches safety_shield.py's tuned-for-this-hardware level


def log(msg: str) -> None:
    print(f"[nav_bridge] {msg}", flush=True)


def _play_start_sound() -> None:
    """Fire-and-forget, on a background thread — a navigation leg should
    never wait on audio playback to actually start moving (this repo
    specifically tuned nav to start snappy earlier this session)."""
    def _run():
        if not os.path.isfile(START_SOUND_FILE):
            log(f"start-sound clip missing: {START_SOUND_FILE}")
            return
        try:
            scale = int(32768 * START_SOUND_VOLUME)
            result = subprocess.run(
                ["mpg123", "-q", "-a", SPEAKER_DEVICE, "-f", str(scale), START_SOUND_FILE],
                timeout=5, capture_output=True, text=True,
            )
            if result.returncode != 0:
                log(f"start-sound mpg123 failed (device={SPEAKER_DEVICE!r}, rc={result.returncode}): "
                    f"{result.stderr.strip()[-300:]}")
        except FileNotFoundError:
            log("start-sound not played — mpg123 not installed")
        except Exception as e:
            log(f"start-sound playback error: {e}")
    threading.Thread(target=_run, daemon=True).start()


def load_waypoint(map_name: str, destination: str) -> dict:
    path = os.path.join(WAYPOINTS_DIR, f"{map_name}.json")
    with open(path, encoding="utf-8") as f:
        waypoints = json.load(f)

    # Primary: case-insensitive match against each entry's "name" field
    # (office_map.json's "Table 1".."Table 5"/"Kitchen"/"Docker" — the same
    # names service_points.label and seed_db.py already use, so callers can
    # just say "Table 3" or "Kitchen" without knowing the underlying id).
    needle = destination.strip().lower()
    for wp in waypoints.values():
        if wp.get("name", "").strip().lower() == needle:
            return wp

    # Fallback: a bare numeric id.
    if destination in waypoints:
        return waypoints[destination]

    raise SystemExit(f"No waypoint named {destination!r} in {path}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Send one blocking NavigateToPose goal and report the result.")
    parser.add_argument("--map", required=True, help="Waypoints file name (without .json), e.g. office_map")
    parser.add_argument("--destination", required=True, help='Waypoint name, e.g. "Table 3" or "Kitchen"')
    parser.add_argument("--timeout", type=float, default=300.0, help="Seconds to wait for the result (default 300)")
    args = parser.parse_args()

    wp = load_waypoint(args.map, args.destination)
    log(f"resolved {args.destination!r} -> x={wp['x']:.3f} y={wp['y']:.3f} "
        f"qz={wp.get('qz', 0.0):.3f} qw={wp.get('qw', 1.0):.3f} (map={args.map})")

    import rclpy
    from action_msgs.msg import GoalStatus
    from geometry_msgs.msg import PoseStamped  # noqa: F401 (documents the field shape used below)
    from nav2_msgs.action import NavigateToPose
    from rclpy.action import ActionClient
    from rclpy.node import Node

    rclpy.init()
    node = Node("sonic_nav_bridge")
    client = ActionClient(node, NavigateToPose, "/navigate_to_pose")

    def finish(result: str) -> None:
        node.destroy_node()
        rclpy.shutdown()
        print(f"RESULT:{result}")
        sys.exit(0 if result == "SUCCESS" else 1)

    server_wait_s = min(10.0, args.timeout)
    log(f"waiting up to {server_wait_s:.0f}s for the /navigate_to_pose action server...")
    t0 = time.monotonic()
    if not client.wait_for_server(timeout_sec=server_wait_s):
        log(f"ERROR: /navigate_to_pose action server not found after {time.monotonic() - t0:.1f}s — "
            "is the Nav2 stack (SLAM + Nav2) actually running? Check with `ros2 action list` in another "
            "terminal (sourced the same way), or start it via the dashboard's 'navigate' mode / "
            "argo_sonic_nav.py first.")
        finish("TIMEOUT")
        return
    log(f"action server found after {time.monotonic() - t0:.1f}s")

    def feedback_cb(feedback_msg):
        dist = feedback_msg.feedback.distance_remaining
        log(f"en route — distance remaining: {dist:.2f} m")

    def send_and_wait() -> str:
        goal = NavigateToPose.Goal()
        goal.pose.header.frame_id = "map"
        goal.pose.header.stamp = node.get_clock().now().to_msg()
        goal.pose.pose.position.x = float(wp["x"])
        goal.pose.pose.position.y = float(wp["y"])
        goal.pose.pose.orientation.z = float(wp.get("qz", 0.0))
        goal.pose.pose.orientation.w = float(wp.get("qw", 1.0))

        log("sending goal...")
        t1 = time.monotonic()
        send_future = client.send_goal_async(goal, feedback_callback=feedback_cb)
        rclpy.spin_until_future_complete(node, send_future, timeout_sec=args.timeout)
        if not send_future.done():
            log(f"ERROR: goal send itself did not complete within {args.timeout:.0f}s "
                "(node may not be discovering the action server over DDS)")
            return "TIMEOUT"

        goal_handle = send_future.result()
        if goal_handle is None or not goal_handle.accepted:
            log("goal REJECTED by Nav2")
            return "REJECTED"
        log(f"goal accepted after {time.monotonic() - t1:.1f}s — navigating "
            f"(up to {args.timeout:.0f}s for a result)...")
        _play_start_sound()

        t2 = time.monotonic()
        result_future = goal_handle.get_result_async()
        rclpy.spin_until_future_complete(node, result_future, timeout_sec=args.timeout)
        if not result_future.done():
            log(f"ERROR: no result within {args.timeout:.0f}s of being accepted "
                f"({time.monotonic() - t2:.1f}s elapsed) — robot may be stuck, Nav2 may be hung, "
                "or --timeout is too short for the distance involved")
            return "TIMEOUT"

        status = result_future.result().status
        log(f"result status={status} (STATUS_SUCCEEDED={GoalStatus.STATUS_SUCCEEDED}) "
            f"after {time.monotonic() - t2:.1f}s")
        return "SUCCESS" if status == GoalStatus.STATUS_SUCCEEDED else "FAILED"

    outcome = send_and_wait()
    if outcome == "REJECTED":
        # One retry on rejection, then give up — bounded, unlike
        # waypoint_manager.py's own infinite-retry loop; this script must
        # report back to sonic_agent.py within --timeout.
        log("retrying once after rejection...")
        outcome = send_and_wait()
        outcome = "FAILED" if outcome == "REJECTED" else outcome

    finish("SUCCESS" if outcome == "SUCCESS" else ("TIMEOUT" if outcome == "TIMEOUT" else "FAILED"))


if __name__ == "__main__":
    main()
