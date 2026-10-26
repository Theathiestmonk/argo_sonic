#!/usr/bin/env python3
"""
nav_lifecycle.py - in-process readiness checks and lifecycle bring-up for the
Argo Sonic Nav2 stack. Run by argo_sonic_nav.py inside the sourced ROS env.

Why this exists: the launcher used to drive every lifecycle transition and
every readiness probe through separate `ros2 ...` CLI processes, each of which
has to rediscover the whole DDS graph and each wrapped in a hard 3-10 s
timeout. On the Jetson under boot load (NTFields loading torch, slam_toolbox
loading the map) those short, load-sensitive calls fail while the nodes are
perfectly healthy, and a timed-out `lifecycle set` was ignored and "retried"
with deactivate/cleanup on a node whose state nobody had verified. This
helper keeps ONE rclpy node alive per operation, calls the lifecycle services
directly, checks every result and the real resulting state, and reports
exactly which dependency failed.

Usage:
  nav_lifecycle.py bringup NODE [--discover-timeout S] [--configure-timeout S]
                    [--activate-timeout S] [--attempts N]
                    [--pre-tf PARENT:CHILD]... [--pre-topic NAME:TYPE]...
                    [--post-action NAME:TYPE]...
  nav_lifecycle.py check [--tf PARENT:CHILD]... [--tf-fresh PARENT:CHILD]...
                    [--topic NAME:TYPE]... [--timeout S]
  nav_lifecycle.py states NODE...
  nav_lifecycle.py pose --out FILE [--timeout S]

Exit status: 0 = everything OK, 1 = a check/transition failed (reason printed
as a single "FAIL ..." line), 2 = usage error.
"""

import argparse
import os
import sys
import threading
import time

import rclpy
from rclpy.action import ActionClient
from rclpy.duration import Duration
from rclpy.executors import SingleThreadedExecutor
from rclpy.node import Node
from rclpy.qos import qos_profile_sensor_data
from rclpy.time import Time
from rosidl_runtime_py.utilities import get_action, get_message
from lifecycle_msgs.msg import State, Transition
from lifecycle_msgs.srv import ChangeState, GetState
from tf2_ros import Buffer, TransformListener

STATE_NAMES = {1: "unconfigured", 2: "inactive", 3: "active", 4: "finalized"}


def say(msg):
    print(f"{time.strftime('%H:%M:%S')} {msg}", flush=True)


class Helper:
    def __init__(self):
        rclpy.init()
        self.node = Node(f"nav_lifecycle_helper_{int(time.time() * 1000) % 100000}")
        self.executor = SingleThreadedExecutor()
        self.executor.add_node(self.node)
        self._spin = threading.Thread(target=self.executor.spin, daemon=True)
        self._spin.start()
        self._tf_buffer = None
        self._tf_listener = None
        self._subs = {}
        self._seen = {}

    def close(self):
        # Stop the spinning thread first: tearing the node/TF listener down
        # while another thread is still inside executor.spin() aborts the
        # process ("terminate called without an active exception").
        try:
            self.executor.shutdown()
            self._spin.join(timeout=3.0)
            if self._tf_listener is not None:
                self._tf_listener.unregister()
            self.node.destroy_node()
            rclpy.shutdown()
        except Exception:
            pass

    # -- generic wait ------------------------------------------------------
    @staticmethod
    def wait_until(pred, timeout, period=0.1):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if pred():
                return True
            time.sleep(period)
        return pred()

    def call(self, client, request, timeout):
        """Call a service and wait up to `timeout`. Never cancels the request:
        a lifecycle transition that is slow must not be abandoned half-done.
        Returns the response, or None if no answer arrived in time."""
        future = client.call_async(request)
        if self.wait_until(future.done, timeout):
            return future.result()
        return None

    # -- TF ----------------------------------------------------------------
    def _ensure_tf(self):
        if self._tf_buffer is None:
            self._tf_buffer = Buffer()
            self._tf_listener = TransformListener(self._tf_buffer, self.node, spin_thread=False)

    def tf_ok(self, parent, child, fresh=None):
        self._ensure_tf()
        try:
            if not self._tf_buffer.can_transform(parent, child, Time(), Duration(seconds=0)):
                return False
            if fresh is not None:
                t = self._tf_buffer.lookup_transform(parent, child, Time())
                age = self.node.get_clock().now() - Time.from_msg(t.header.stamp)
                return age.nanoseconds / 1e9 <= fresh
            return True
        except Exception:
            return False

    # -- topic data ---------------------------------------------------------
    def _watch_topic(self, name, type_str):
        if name in self._subs:
            return
        msg_type = get_message(type_str)
        self._seen[name] = 0
        self._subs[name] = self.node.create_subscription(
            msg_type, name, lambda _m, n=name: self._seen.__setitem__(n, self._seen[n] + 1),
            qos_profile_sensor_data)

    def topic_has_data(self, name):
        return self._seen.get(name, 0) > 0

    # -- lifecycle ---------------------------------------------------------
    def lifecycle_state(self, get_client, timeout=5.0):
        res = self.call(get_client, GetState.Request(), timeout)
        return None if res is None else res.current_state.id

    def wait_stable_state(self, get_client, timeout):
        """Wait until the node is not mid-transition (state id <= 4)."""
        last = [None]

        def stable():
            s = self.lifecycle_state(get_client, 3.0)
            last[0] = s
            return s is not None and s in STATE_NAMES
        self.wait_until(stable, timeout, period=0.3)
        return last[0]

    def check(self, tfs, tfs_fresh, topics, timeout):
        """All listed dependencies must be satisfied within `timeout` (they are
        polled in parallel, so the timeout bounds the whole check)."""
        for name, type_str in topics:
            self._watch_topic(name, type_str)
        pending = {("tf", p, c, None) for p, c in tfs} | {("tf", p, c, 2.0) for p, c in tfs_fresh} \
            | {("topic", n, t, None) for n, t in topics}
        done_at = {}
        t0 = time.time()
        deadline = t0 + timeout
        while pending and time.time() < deadline:
            for item in list(pending):
                kind, a, b, fresh = item
                ok = self.tf_ok(a, b, fresh) if kind == "tf" else self.topic_has_data(a)
                if ok:
                    pending.discard(item)
                    done_at[item] = time.time() - t0
                    say(f"OK   {self._label(item)} ({done_at[item]:.1f}s)")
            time.sleep(0.1)
        for item in sorted(pending, key=str):
            say(f"FAIL {self._label(item)} not satisfied within {timeout:.0f}s")
        return not pending

    @staticmethod
    def _label(item):
        kind, a, b, fresh = item
        if kind == "tf":
            return f"TF {a}->{b}" + (" (fresh)" if fresh else "")
        return f"data on {a}"

    def capture_pose(self, out_path, timeout):
        """Write the robot's last localized pose (slam_toolbox's /pose, else
        /odom) to out_path as JSON. One in-process subscription: ~1 s instead
        of the several 5 s CLI probes shutdown used to need."""
        import json
        from geometry_msgs.msg import PoseWithCovarianceStamped
        from nav_msgs.msg import Odometry
        got = {}

        def on_pose(m):
            got.setdefault("pose", m.pose.pose)

        def on_odom(m):
            got.setdefault("odom", m.pose.pose)
        self.node.create_subscription(PoseWithCovarianceStamped, "/pose", on_pose, qos_profile_sensor_data)
        self.node.create_subscription(Odometry, "/odom", on_odom, qos_profile_sensor_data)
        self.wait_until(lambda: "pose" in got, timeout)
        src = "pose" if "pose" in got else ("odom" if "odom" in got else None)
        if src is None:
            say("FAIL no /pose or /odom message received")
            return False
        q = got[src]
        with open(out_path, "w") as f:
            json.dump({"x": q.position.x, "y": q.position.y, "qz": q.orientation.z,
                       "qw": q.orientation.w, "saved_at": time.time()}, f)
        say(f"OK   saved {src} ({q.position.x:.2f}, {q.position.y:.2f})")
        return True

    def bringup(self, node_name, args):
        name = node_name if node_name.startswith("/") else "/" + node_name
        get_c = self.node.create_client(GetState, f"{name}/get_state")
        chg_c = self.node.create_client(ChangeState, f"{name}/change_state")

        if not (get_c.wait_for_service(timeout_sec=args.discover_timeout)
                and chg_c.wait_for_service(timeout_sec=5.0)):
            say(f"FAIL {name}: lifecycle services not available after "
                f"{args.discover_timeout:.0f}s (process crashed or never started?)")
            return False

        state = self.wait_stable_state(get_c, args.configure_timeout)
        say(f"INFO {name}: current state {STATE_NAMES.get(state, state)}")
        if state is None:
            say(f"FAIL {name}: get_state returned nothing")
            return False
        if state == State.PRIMARY_STATE_FINALIZED:
            say(f"FAIL {name}: node is finalized and cannot be used")
            return False

        for attempt in range(1, args.attempts + 1):
            if state == State.PRIMARY_STATE_UNCONFIGURED:
                t0 = time.time()
                res = self.call(chg_c, self._req(Transition.TRANSITION_CONFIGURE), args.configure_timeout)
                if res is None:
                    say(f"FAIL {name}: configure did not return within {args.configure_timeout:.0f}s")
                    return False
                state = self.wait_stable_state(get_c, 10.0)
                if not res.success or state != State.PRIMARY_STATE_INACTIVE:
                    # No retry: a configure failure (bad plugin/parameter, missing
                    # model, ...) is deterministic, and re-running configure on a
                    # half-built Nav2 costmap/controller was observed to hang.
                    say(f"FAIL {name}: configure failed "
                        f"(success={res.success}, state={STATE_NAMES.get(state, state)}) "
                        f"- see this node's log for the plugin/parameter error")
                    return False
                say(f"OK   {name}: configured ({time.time() - t0:.1f}s)")

            if state == State.PRIMARY_STATE_INACTIVE:
                if not self._prechecks(name, args):
                    return False
                t0 = time.time()
                res = self.call(chg_c, self._req(Transition.TRANSITION_ACTIVATE), args.activate_timeout)
                if res is None:
                    say(f"FAIL {name}: activate did not return within {args.activate_timeout:.0f}s")
                    return False
                state = self.wait_stable_state(get_c, 10.0)
                if not res.success or state != State.PRIMARY_STATE_ACTIVE:
                    say(f"WARN {name}: activate attempt {attempt}/{args.attempts} failed "
                        f"(success={res.success}, state={STATE_NAMES.get(state, state)})")
                    if attempt == args.attempts or state != State.PRIMARY_STATE_INACTIVE:
                        say(f"FAIL {name}: could not activate")
                        return False
                    time.sleep(1.0)
                    continue
                say(f"OK   {name}: active ({time.time() - t0:.1f}s)")

            if state == State.PRIMARY_STATE_ACTIVE:
                break

        if state != State.PRIMARY_STATE_ACTIVE:
            say(f"FAIL {name}: ended in state {STATE_NAMES.get(state, state)}")
            return False

        for act in args.post_action:
            aname, atype = act.rsplit(":", 1)
            client = ActionClient(self.node, get_action(atype), aname)
            if not client.wait_for_server(timeout_sec=30.0):
                say(f"FAIL {name}: active, but action server {aname} is not available")
                return False
            say(f"OK   action {aname} available")
        return True

    def _prechecks(self, name, args):
        tfs = [tuple(x.split(":", 1)) for x in args.pre_tf]
        topics = [tuple(x.split(":", 1)) for x in args.pre_topic]
        if not (tfs or topics):
            return True
        say(f"INFO {name}: checking dependencies before activation")
        return self.check(tfs, [], topics, args.pre_timeout)

    @staticmethod
    def _req(transition_id):
        req = ChangeState.Request()
        req.transition.id = transition_id
        return req


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("bringup")
    b.add_argument("node")
    b.add_argument("--discover-timeout", type=float, default=60.0)
    b.add_argument("--configure-timeout", type=float, default=90.0)
    b.add_argument("--activate-timeout", type=float, default=60.0)
    b.add_argument("--attempts", type=int, default=2,
                   help="activation attempts (configure is never retried)")
    b.add_argument("--pre-tf", action="append", default=[])
    b.add_argument("--pre-topic", action="append", default=[])
    b.add_argument("--pre-timeout", type=float, default=45.0)
    b.add_argument("--post-action", action="append", default=[])

    c = sub.add_parser("check")
    c.add_argument("--tf", action="append", default=[])
    c.add_argument("--tf-fresh", action="append", default=[])
    c.add_argument("--topic", action="append", default=[])
    c.add_argument("--timeout", type=float, default=30.0)

    pz = sub.add_parser("pose")
    pz.add_argument("--out", required=True)
    pz.add_argument("--timeout", type=float, default=2.5)

    s = sub.add_parser("states")
    s.add_argument("nodes", nargs="+")

    args = ap.parse_args()
    h = Helper()
    try:
        if args.cmd == "bringup":
            ok = h.bringup(args.node, args)
        elif args.cmd == "check":
            ok = h.check([tuple(x.split(":", 1)) for x in args.tf],
                         [tuple(x.split(":", 1)) for x in args.tf_fresh],
                         [tuple(x.split(":", 1)) for x in args.topic], args.timeout)
        elif args.cmd == "pose":
            ok = h.capture_pose(args.out, args.timeout)
        else:
            ok = True
            for n in args.nodes:
                name = n if n.startswith("/") else "/" + n
                gc = h.node.create_client(GetState, f"{name}/get_state")
                if not gc.wait_for_service(timeout_sec=5.0):
                    say(f"FAIL {name}: not found")
                    ok = False
                    continue
                st = h.lifecycle_state(gc, 5.0)
                say(f"{'OK  ' if st == State.PRIMARY_STATE_ACTIVE else 'FAIL'} {name}: "
                    f"{STATE_NAMES.get(st, st)}")
                ok = ok and st == State.PRIMARY_STATE_ACTIVE
    finally:
        h.close()
    # Exit immediately: all work is done and rclcpp's interpreter-teardown
    # can still abort on some builds; the exit status below is what callers read.
    sys.stdout.flush()
    os._exit(0 if ok else 1)


if __name__ == "__main__":
    main()
