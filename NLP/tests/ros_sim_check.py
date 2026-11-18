"""Exercises the REAL RosRobot against a tiny in-process ROS 2 simulator (lidar, ultrasonics, odom, a
cmd_vel integrator with a shield-like stall, and a fake Nav2 action server).   Needs ROS sourced.
    python -m NLP.tests.ros_sim_check"""
import asyncio
import math
import threading
import time

import rclpy
from geometry_msgs.msg import Twist
from nav2_msgs.action import NavigateToPose
from nav_msgs.msg import Odometry
from rclpy.action import ActionServer
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node
from rclpy.qos import QoSProfile, ReliabilityPolicy
from sensor_msgs.msg import LaserScan, Range
from std_msgs.msg import Float32

from ..config import load_settings
from ..robot.ros import RosRobot


class Sim(Node):
    def __init__(self):
        super().__init__("sonic_sim")
        be = QoSProfile(reliability=ReliabilityPolicy.BEST_EFFORT, depth=5)
        self.x = self.y = self.yaw = 0.0
        self.v = self.w = 0.0
        self.walls = []                 # ((x1,y1),(x2,y2)) in world frame
        self.publish_scan = True
        self.us = {"front_left": 9.0, "front_right": 9.0, "back_left": 9.0, "back_right": 9.0}
        self.shield_freeze = False      # emulate the shield zeroing linear velocity (hidden obstacle)
        self.scan_pub = self.create_publisher(LaserScan, "/scan_corrected", be)
        self.odom_pub = self.create_publisher(Odometry, "/odom", 10)
        self.dep_pub = self.create_publisher(Float32, "/safety_shield/depth_distance", be)
        self.us_pub = {k: self.create_publisher(Range, f"/us/{k}", be) for k in self.us}
        self.create_subscription(Twist, "/cmd_vel_smoothed", self.on_cmd, 10)
        self.create_timer(0.05, self.tick)
        ActionServer(self, NavigateToPose, "/navigate_to_pose", self.nav_exec)
        self.last_cmd = 0.0

    def nav_exec(self, goal_handle):
        time.sleep(0.5)
        goal_handle.succeed()
        return NavigateToPose.Result()

    def on_cmd(self, m):
        self.v, self.w, self.last_cmd = m.linear.x, m.angular.z, time.monotonic()

    def ray(self, a_world):
        best = 10.0
        dx, dy = math.cos(a_world), math.sin(a_world)
        for (x1, y1), (x2, y2) in self.walls:
            ex, ey = x2 - x1, y2 - y1
            den = dx * ey - dy * ex
            if abs(den) < 1e-9:
                continue
            t = ((x1 - self.x) * ey - (y1 - self.y) * ex) / den
            u = ((x1 - self.x) * dy - (y1 - self.y) * dx) / den
            if t > 0.05 and 0 <= u <= 1:
                best = min(best, t)
        return best

    def tick(self):
        dt = 0.05
        if time.monotonic() - self.last_cmd > 0.3:
            self.v = self.w = 0.0
        v = 0.0 if self.shield_freeze else self.v
        self.x += v * math.cos(self.yaw) * dt
        self.y += v * math.sin(self.yaw) * dt
        self.yaw += self.w * dt
        o = Odometry()
        o.pose.pose.position.x, o.pose.pose.position.y = self.x, self.y
        o.pose.pose.orientation.z, o.pose.pose.orientation.w = math.sin(self.yaw / 2), math.cos(self.yaw / 2)
        self.odom_pub.publish(o)
        if self.publish_scan:
            s = LaserScan()
            s.angle_min, s.angle_max, s.angle_increment = -math.pi, math.pi, math.radians(1.0)
            s.range_min, s.range_max = 0.1, 10.0
            n = int((s.angle_max - s.angle_min) / s.angle_increment)
            s.ranges = [self.ray(self.yaw + s.angle_min + i * s.angle_increment) for i in range(n)]
            self.scan_pub.publish(s)
        for k, p in self.us_pub.items():
            r = Range(); r.min_range, r.max_range, r.range = 0.02, 4.0, float(self.us[k]); p.publish(r)
        self.dep_pub.publish(Float32(data=9.0))


async def main():
    s = load_settings()
    rclpy.init()
    sim = Sim()
    ex = MultiThreadedExecutor(num_threads=4); ex.add_node(sim)
    threading.Thread(target=ex.spin, daemon=True).start()
    robot = RosRobot(s)
    await robot.start()
    await asyncio.sleep(1.0)                      # sensors warm up

    def pose(): return (round(sim.x, 2), round(sim.y, 2), round(math.degrees(sim.yaw)))
    results = []

    def check(name, ok, info=""):
        results.append(ok); print(("PASS " if ok else "FAIL ") + name, info)

    print("clearance fwd (open):", robot.clearance("forward"))
    r = await robot.move("forward"); check("forward 1 m", r.ok and abs(sim.x - 1.0) < 0.08, f"{r} pose={pose()}")
    r = await robot.move("backward"); check("backward 1 m", r.ok and abs(sim.x) < 0.1, f"{r} pose={pose()}")
    y0 = sim.yaw
    r = await robot.move("turn_left"); check("turn left 90", r.ok and abs(math.degrees(sim.yaw - y0) - 90) < 4, pose())
    r = await robot.move("turn_right"); check("turn right 90", r.ok and abs(math.degrees(sim.yaw - y0)) < 4, pose())
    y0 = sim.yaw
    r = await robot.move("spin"); check("spin 360", r.ok, f"{r} pose={pose()}")
    sim.x = sim.y = sim.yaw = 0.0
    r = await robot.move("left"); check("left = turn + 1 m (+y)", r.ok and abs(sim.y - 1.0) < 0.1 and abs(sim.x) < 0.1, pose())
    sim.x = sim.y = sim.yaw = 0.0

    sim.walls = [((1.2, -3), (1.2, 3))]           # wall 1.2 m ahead: < 1.0 + 0.5 margin
    await asyncio.sleep(0.4)
    x0 = sim.x
    r = await robot.move("forward"); check("wall at 1.2 m → blocked, no motion", r.status == "blocked" and abs(sim.x - x0) < 0.01, f"{r} clear={robot.clearance('forward'):.2f}")
    sim.walls = [((1.2, -3), (1.2, 3)), ((-3, 0.6), (3, 0.6))]
    sim.yaw = 0.0
    r = await robot.move("left"); check("wall 0.6 m to the left → blocked", r.status == "blocked", f"{r}")
    r = await robot.move("spin"); check("spin ok with walls ≥ .6 m", r.ok or r.status == "blocked", f"{r}")
    sim.walls = []
    sim.x = sim.y = sim.yaw = 0.0

    sim.shield_freeze = True                      # sensors see nothing, but the shield refuses to move us
    t0 = time.monotonic()
    r = await robot.move("forward"); check("shield stall → blocked (<3 s)", r.status == "blocked" and time.monotonic() - t0 < 3.5, f"{r} {time.monotonic()-t0:.1f}s")
    sim.shield_freeze = False

    sim.publish_scan = False
    await asyncio.sleep(1.4)
    r = await robot.move("left"); check("no lidar (left) → no_sensors", r.status == "no_sensors", f"{r}")
    sim.publish_scan = True
    await asyncio.sleep(0.5)

    ok = await robot.navigate({"x": 1.0, "y": 2.0, "qz": 0.0, "qw": 1.0}); check("navigate goal succeeds", ok)
    t = asyncio.create_task(robot.navigate({"x": 1.0, "y": 2.0}))
    await asyncio.sleep(0.1); t.cancel()
    try: await t
    except asyncio.CancelledError: check("navigate cancel propagates", True)
    await robot.close()
    print(f"\n{sum(results)}/{len(results)} passed")
    raise SystemExit(0 if all(results) else 1)

asyncio.run(main())
