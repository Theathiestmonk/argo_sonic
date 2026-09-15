#!/usr/bin/env python3
"""Build the ceiling landmark map, and measure the one mount angle a single
view of the ceiling cannot see.

`ceiling_features` reports lights in metres on the ceiling plane, in a basis
tied rigidly to the camera. That basis is metric and it does not rotate with
the robot, but *where it points* relative to `base_link` is unknown: a flat
ceiling viewed from one pose maps onto itself under rotation about vertical, so
`ceiling_calibrate` can measure the mount's tilt and lean and nothing more. The
missing angle only shows up once the robot moves, which is why it is measured
here.

Two jobs, in order, from one drive:

  1. Azimuth.  Drive the robot; the landmark cloud slides and turns in the
     ceiling frame while odometry says how the robot slid and turned. Those are
     the same motion seen in two frames, so the rotation between the frames
     falls out of a hand-eye solve (X·A = B·X). Pure translation pins the
     angle, rotation pins the lever arm, and the drive supplies both.
  2. Map.  With the angle known, every observation lands in the map frame, and
     landmarks are accumulated as weighted running means.

The keyframes gathered during 1 are replayed into 2, so the calibration drive
*is* the mapping drive — there is no separate step.

The map is built in the laser `map` frame, seeded by whatever is localising
against the laser map, so the ceiling map is born registered to it and to the
table waypoints stored alongside it. Within a keyframe the pose is refined
against the ceiling map itself (that is the whole point — ceiling geometry is
more repeatable than a laser map built on furniture), but the refinement is
clamped: allowed to correct the laser pose, not to walk the ceiling map out of
the frame the waypoints live in.

Usage:
    ros2 run argo_mini ceiling_map_builder
    ros2 run argo_mini ceiling_map_builder --ros-args \
        -p map_path:=/abs/path/Atsn_cafe_map.ceiling.json

Drive the robot around the room — mostly straight runs, with some turns, and
revisit places — then Ctrl-C. The map is saved on the way out and every
`save_period_s` before that. Feed the azimuth it prints back to
`ceiling_calibrate -p ceiling_azimuth_deg:=<value>` for the final mount TF.
"""

import json
import math
import os
import time

import numpy as np
import rclpy
from rclpy.node import Node
from rclpy.qos import QoSPresetProfiles
from geometry_msgs.msg import Point32
from sensor_msgs.msg import ChannelFloat32, PointCloud
from tf2_ros import Buffer, TransformListener


# ── SE(2) ───────────────────────────────────────────────────────────────
# A pose is (x, y, theta). Everything here is planar: the ceiling is flat and
# the floor is flat, so a landmark's position on the ceiling and the robot's
# position on the floor are related by a rigid 2D transform and nothing else.

def wrap(a):
    return (a + np.pi) % (2 * np.pi) - np.pi


def rot(t):
    c, s = np.cos(t), np.sin(t)
    return np.array([[c, -s], [s, c]])


def xform(p, pts):
    """Transform an Nx2 array of points by pose p."""
    return np.asarray(pts) @ rot(p[2]).T + p[:2]


def compose(a, b):
    t = rot(a[2]) @ b[:2] + a[:2]
    return np.array([t[0], t[1], wrap(a[2] + b[2])])


def invert(p):
    t = -(rot(-p[2]) @ p[:2])
    return np.array([t[0], t[1], wrap(-p[2])])


def fit_rigid(src, dst, w, fix_theta=False, theta=0.0):
    """Weighted 2D rigid transform taking src onto dst.

    `fix_theta` holds the angle and solves translation only, for when the
    matched landmarks are too close together to say anything about rotation —
    two lights half a metre apart will happily invent several degrees.
    """
    W = w.sum()
    ms = (src * w[:, None]).sum(0) / W
    md = (dst * w[:, None]).sum(0) / W
    if fix_theta:
        th = theta
    else:
        a, b = src - ms, dst - md
        num = float((w * (a[:, 0] * b[:, 1] - a[:, 1] * b[:, 0])).sum())
        den = float((w * (a[:, 0] * b[:, 0] + a[:, 1] * b[:, 1])).sum())
        th = math.atan2(num, den)
    t = md - rot(th) @ ms
    return np.array([t[0], t[1], wrap(th)])


def icp(src, dst, w, init, gate, min_span=0.5, iters=12, anneal=2.0):
    """Align src onto dst from `init`. Returns (pose, n_pairs, rms).

    Correspondences are mutual nearest neighbours: a ceiling of lights is
    sparse and near-regular, so one-way matching happily collapses several
    observations onto one map landmark and then reports a confident, wrong fit.

    The gate anneals from `anneal * gate` down to `gate` over the first half of
    the iterations. A fixed gate has to choose between capture radius and
    precision, and here it cannot win either way: a few degrees of initial
    heading error throws a landmark 4 m out across the room further than the
    gate that the final fit wants to be held to. Annealing keeps both, and
    starting at twice the association gate stays well inside half the spacing
    of a light grid, so it never buys capture range by hopping a grid cell.
    """
    p = np.array(init, float)
    rows = np.arange(len(src))
    out = (None, 0, float('inf'))
    for it in range(iters):
        g = gate * anneal ** max(0.0, 1.0 - 2.0 * it / max(1, iters - 1))
        d = np.linalg.norm(xform(p, src)[:, None, :] - dst[None, :, :], axis=2)
        j = d.argmin(1)
        keep = (d[rows, j] < g) & (d.argmin(0)[j] == rows)
        k = int(keep.sum())
        if k < 2:
            if g > gate * 1.001:      # still annealing — give it another pass
                continue
            return None, k, float('inf')
        s, t, ww = src[keep], dst[j[keep]], w[keep]
        span = float(np.ptp(xform(p, s), axis=0).max())
        q = fit_rigid(s, t, ww, fix_theta=span < min_span, theta=p[2])
        step = float(np.linalg.norm(q[:2] - p[:2])) + abs(wrap(q[2] - p[2]))
        p = q
        res = np.linalg.norm(xform(p, s) - t, axis=1)
        out = (p.copy(), k, float(np.sqrt((res ** 2 * ww).sum() / ww.sum())))
        if step < 1e-4:
            break
    return out


def solve_hand_eye_fixed_lever(pairs, t_x):
    """Solve for psi alone, with the lever arm taken as known.

    This is the well-conditioned half of the problem, and on a sparse ceiling it
    is the one worth solving. `R(psi)·t_a = (R_b - I)·t_x + t_b` has a fully
    known right-hand side once t_x is given, leaving two rows per pair linear in
    (cos psi, sin psi) — one effective unknown, over-determined by every pair.

    Letting t_x float instead costs more than it looks. Its columns are
    `R_b - I`, whose magnitude is the per-pair rotation: on the drive that
    prompted this, ~0.09 rad against |t_a| ~0.2 m for the angle columns. So the
    lever arm is the weakly observed part, and least squares will happily spend
    those two free parameters absorbing noise — which then leaks into psi, the
    quantity actually wanted. Since the camera is bolted at a known offset,
    handing the solver that fact removes the leak rather than adding an
    assumption.
    """
    M, rhs = [], []
    for A, B in pairs:
        ax, ay = float(A[0]), float(A[1])
        r = (rot(B[2]) - np.eye(2)) @ np.asarray(t_x, float) \
            + np.asarray(B[:2], float)
        M.append([ax, -ay]); rhs.append(float(r[0]))
        M.append([ay, ax]);  rhs.append(float(r[1]))
    M, rhs = np.asarray(M), np.asarray(rhs)
    sol, *_ = np.linalg.lstsq(M, rhs, rcond=None)
    psi = math.atan2(sol[1], sol[0])
    unit = np.array([math.cos(psi), math.sin(psi)])
    resid = float(np.sqrt(np.mean((M @ unit - rhs) ** 2)))
    return psi, np.asarray(t_x, float), resid


def solve_hand_eye(pairs):
    """Solve X·A = B·X for X = (R(psi), t_x) over 2D motion pairs.

    A is the ceiling frame's own motion between two keyframes, B the robot's
    over the same interval, and X the fixed base_link -> ceiling-frame
    transform we are after. In 2D the rotation halves of X·A = B·X reduce to
    theta_a == theta_b, which carries no information about X but makes an
    excellent gate on the input. The translation halves,

        R(psi)·t_a = (R_b - I)·t_x + t_b,

    are linear in (cos psi, sin psi, t_x), two rows per pair. Note the two
    unknowns separate by motion type: under pure translation R_b - I vanishes
    and the pair speaks only about psi; under rotation t_x dominates. A drive
    that only ever goes straight will nail the angle and leave the lever arm
    unobserved, which is why they are reported with separate confidence.
    """
    M, rhs = [], []
    for A, B in pairs:
        ax, ay = float(A[0]), float(A[1])
        D = rot(B[2]) - np.eye(2)
        M.append([ax, -ay, -D[0, 0], -D[0, 1]])
        M.append([ay, ax, -D[1, 0], -D[1, 1]])
        rhs.extend([float(B[0]), float(B[1])])
    M, rhs = np.asarray(M), np.asarray(rhs)
    sol, *_ = np.linalg.lstsq(M, rhs, rcond=None)

    # The solve treats (cos psi, sin psi) as free, so renormalise, then redo
    # the lever arm with the angle pinned rather than letting a scale error
    # leak into it.
    psi = math.atan2(sol[1], sol[0])
    rows, rr = [], []
    for A, B in pairs:
        D = rot(B[2]) - np.eye(2)
        rows.append(D[0])
        rows.append(D[1])
        v = rot(psi) @ np.asarray(A[:2], float) - np.asarray(B[:2], float)
        rr.extend([v[0], v[1]])
    rows, rr = np.asarray(rows), np.asarray(rr)
    t_x, *_ = np.linalg.lstsq(rows, rr, rcond=None)

    full = np.concatenate([[math.cos(psi), math.sin(psi)], t_x])
    resid = float(np.sqrt(np.mean((M @ full - rhs) ** 2)))
    return psi, t_x, resid


# ── landmarks ───────────────────────────────────────────────────────────

class Landmark:
    """One ceiling light, as a weighted running mean in the map frame.

    The spread kept here is of the observations *as mapped*, so it folds in
    pose error as well as detector noise — which is what makes it the useful
    number: it says how repeatably this light lands, not how crisp the blob is.
    """
    __slots__ = ('sw', 'sx', 'sy', 'sxx', 'syy', 'n', 'last')

    def __init__(self):
        self.sw = self.sx = self.sy = self.sxx = self.syy = 0.0
        self.n = 0
        self.last = 0.0

    def add(self, x, y, w, stamp):
        self.sw += w
        self.sx += w * x
        self.sy += w * y
        self.sxx += w * x * x
        self.syy += w * y * y
        self.n += 1
        self.last = stamp

    @property
    def xy(self):
        return np.array([self.sx / self.sw, self.sy / self.sw])

    @property
    def sigma(self):
        mx, my = self.sx / self.sw, self.sy / self.sw
        v = max(0.0, self.sxx / self.sw - mx * mx) + \
            max(0.0, self.syy / self.sw - my * my)
        return math.sqrt(v)


class CeilingMapBuilder(Node):
    def __init__(self):
        super().__init__('ceiling_map_builder')

        self.declare_parameter('lights_topic', '/ceiling_features/lights')
        self.declare_parameter('map_frame', 'map')
        self.declare_parameter('odom_frame', 'odom')
        self.declare_parameter('base_frame', 'base_link')
        # Resolved against the shell's cwd and echoed absolute at startup, so
        # `ros2 run` from anywhere still says plainly where the map will land.
        self.declare_parameter('map_path', 'maps/ceiling_map.json')
        self.declare_parameter('extend_existing', False)

        # Supply this to skip the calibration phase and go straight to mapping
        # — a second run against the same mount does not need to re-measure it.
        self.declare_parameter('ceiling_azimuth_deg', float('nan'))
        # From the URDF camera_joint: the lever arm to fall back on when the
        # drive has too little rotation to measure one.
        self.declare_parameter('camera_xy', [0.2575, 0.0])
        self.declare_parameter('camera_z', 0.170)

        # A keyframe every 0.2 m keeps successive clouds overlapping while
        # still moving far enough that the motion is signal and not noise.
        self.declare_parameter('keyframe_dist', 0.20)
        self.declare_parameter('keyframe_rot_deg', 8.0)
        self.declare_parameter('min_lights', 2)
        self.declare_parameter('max_incidence_deg', 45.0)

        self.declare_parameter('min_pairs', 25)
        self.declare_parameter('rot_consistency_deg', 3.0)
        self.declare_parameter('pair_rms_max', 0.10)
        # Rotation summed over accepted pairs, below which the lever arm is
        # not believed and the URDF value is kept instead.
        self.declare_parameter('lever_excitation_rad', 1.0)
        # Two sanity checks on the hand-eye result, because its failure mode is
        # to return a confident wrong number rather than to fail.
        #
        # The residual is the solve's own fit to its own data. In simulation it
        # lands at 4.8 mm; a live run on a ceiling showing only 2 landmarks --
        # one of them a glow pool the detector should not have published --
        # returned 90.7 mm and an azimuth 13 deg from the installed value.
        #
        # The lever arm is the stronger test, because it is the one quantity
        # here that can be checked against a tape measure: the camera is bolted
        # to the robot at the URDF offset and cannot be anywhere else. That same
        # run claimed [+0.636 -0.288] m against a URDF [+0.2575 0.0000] — 0.48 m
        # away, physically impossible. A solve that gets a measurable quantity
        # that wrong is not to be trusted on the one you cannot measure.
        self.declare_parameter('max_residual_mm', 25.0)
        self.declare_parameter('max_lever_error_m', 0.15)
        # How far |t_a| may sit from what odometry and the bolted camera offset
        # say it must be. Keyframes are ~0.20 m, landmark jitter is ~2 mm, and
        # the URDF offset is good to a couple of cm, so honest pairs land well
        # inside 60 mm; the bad ones measured here were 80-150 mm out.
        self.declare_parameter('max_translation_mismatch_m', 0.06)
        # Let the solve estimate the lever arm instead of taking the bolted
        # URDF value. Off by default: it is the weakly observed part of the
        # problem and the two free parameters mostly absorb noise that then
        # leaks into the azimuth. Worth turning on only on a dense ceiling
        # driven with a lot of rotation, or to audit the URDF value itself.
        self.declare_parameter('estimate_lever', False)

        self.declare_parameter('track_gate', 0.60)   # keyframe-to-keyframe ICP
        self.declare_parameter('assoc_gate', 0.30)   # observation -> map
        self.declare_parameter('min_span', 0.50)
        self.declare_parameter('min_observations', 5)
        self.declare_parameter('max_anchor_xy', 0.50)
        self.declare_parameter('max_anchor_yaw_deg', 15.0)
        self.declare_parameter('save_period_s', 20.0)

        g = lambda k: self.get_parameter(k).value
        self.map_frame = g('map_frame')
        self.odom_frame = g('odom_frame')
        self.base_frame = g('base_frame')
        self.map_path = os.path.abspath(g('map_path'))
        self.kf_dist = float(g('keyframe_dist'))
        self.kf_rot = math.radians(float(g('keyframe_rot_deg')))
        self.min_lights = int(g('min_lights'))
        self.max_inc = float(g('max_incidence_deg'))
        self.min_pairs = int(g('min_pairs'))
        self.rot_tol = math.radians(float(g('rot_consistency_deg')))
        self.pair_rms_max = float(g('pair_rms_max'))
        self.lever_exc = float(g('lever_excitation_rad'))
        self.max_resid_mm = float(g('max_residual_mm'))
        self.max_lever_err = float(g('max_lever_error_m'))
        self.max_t_mismatch = float(g('max_translation_mismatch_m'))
        self.estimate_lever = bool(self.get_parameter('estimate_lever').value)
        self.camera_xy = np.array([float(v) for v in
                                   self.get_parameter('camera_xy').value])
        self.rej_translation = 0
        self.solve_at = None     # pair count at which to (re)try the solve
        self.track_gate = float(g('track_gate'))
        self.assoc_gate = float(g('assoc_gate'))
        self.min_span = float(g('min_span'))
        self.min_obs = int(g('min_observations'))
        self.max_anchor_xy = float(g('max_anchor_xy'))
        self.max_anchor_yaw = math.radians(float(g('max_anchor_yaw_deg')))
        self.camera_z = float(g('camera_z'))

        # X = base_link -> ceiling frame. None until measured or supplied.
        self.X = None
        self.lever_source = 'urdf'
        az = float(g('ceiling_azimuth_deg'))
        if not math.isnan(az):
            self.X = np.array([*[float(v) for v in g('camera_xy')],
                               math.radians(az)])
            self.lever_source = 'parameter'

        self.pairs = []          # (A, B) motion pairs for the hand-eye solve
        self.excitation = 0.0    # total |rotation| across accepted pairs
        self.pending = []        # keyframes held back until the angle is known
        self.prev = None         # (pose, pts, w) of the previous keyframe
        self.kf_pose = None      # robot pose at the previous keyframe
        self.marks = []          # Landmark, map frame
        self.correction = np.zeros(3)   # last accepted ceiling-vs-laser offset
        self.height_sum, self.height_n = 0.0, 0
        self.frames = 0
        self.pose_frame = None
        self.hand_eye = None

        self.tf = Buffer()
        self.tf_listener = TransformListener(self.tf, self)

        self.create_subscription(PointCloud, g('lights_topic'), self.on_lights,
                                 QoSPresetProfiles.SENSOR_DATA.value)
        self.pub_map = self.create_publisher(PointCloud, '~/map', 1)
        self.create_timer(float(g('save_period_s')), self.save)

        if bool(g('extend_existing')):
            self.load()
        elif os.path.exists(self.map_path):
            self.get_logger().warn(
                f'{self.map_path} exists and will be overwritten on save; '
                'pass -p extend_existing:=true to add to it instead')

        self.get_logger().info(f'ceiling_map_builder up — map -> {self.map_path}')
        if self.X is None:
            self.get_logger().info(
                f'azimuth unknown: drive the robot, {self.min_pairs} motion '
                'pairs needed (straight runs plus some turns)')
        else:
            self.get_logger().info(
                f'azimuth given as {math.degrees(self.X[2]):+.2f} deg — mapping now')

    # ── input ───────────────────────────────────────────────────────────
    def on_lights(self, msg):
        pts, w, hgt = self.unpack(msg)
        if len(pts) < self.min_lights:
            return
        pose = self.robot_pose(msg.header.stamp)
        if pose is None:
            return
        if self.kf_pose is not None:
            d = compose(invert(self.kf_pose), pose)
            if np.linalg.norm(d[:2]) < self.kf_dist and abs(d[2]) < self.kf_rot:
                return
        self.kf_pose = pose
        self.frames += 1
        if hgt is not None:
            self.height_sum += hgt
            self.height_n += 1

        if self.X is None:
            self.calibrate_step(pose, pts, w)
        else:
            self.insert(pose, pts, w)
            self.publish_map()

    def unpack(self, msg):
        """PointCloud -> (Nx2 points, weights, ceiling height above camera).

        Incidence is the detector's own quality figure — at 45 deg the ceiling
        is 9.2 mm/px against 6.3 mm/px underfoot, and any fixture standoff is
        multiplied by tan() — so it becomes the weight directly. Height comes
        free: range * cos(incidence) is the perpendicular distance to the
        plane, so the two channels recover what the detector measured.
        """
        ch = {c.name: np.asarray(c.values, float) for c in msg.channels}
        n = len(msg.points)
        inc = ch.get('incidence_deg', np.zeros(n))
        rng = ch.get('range_m')
        pts = np.array([[p.x, p.y] for p in msg.points], float).reshape(n, 2)
        ok = inc <= self.max_inc
        c = np.cos(np.radians(inc))
        hgt = float(np.median(rng[ok] * c[ok])) if rng is not None and ok.any() \
            else None
        return pts[ok], np.maximum(c[ok] ** 2, 1e-3), hgt

    def robot_pose(self, stamp):
        """Robot pose in the map frame, falling back to odom.

        Without the laser map the ceiling map is still perfectly self
        consistent — it is just expressed in a frame that drifts and that the
        table waypoints know nothing about, so say so loudly.
        """
        for frame in (self.map_frame, self.odom_frame):
            for t in (stamp, rclpy.time.Time()):
                try:
                    tr = self.tf.lookup_transform(
                        frame, self.base_frame, t,
                        timeout=rclpy.duration.Duration(seconds=0.05))
                except Exception:
                    continue
                if self.pose_frame is None:
                    self.pose_frame = frame
                    if frame != self.map_frame:
                        self.get_logger().warn(
                            f'no {self.map_frame} -> {self.base_frame}; building '
                            f'in {frame} instead. The map will not be registered '
                            'to the laser map or to the table waypoints.')
                if frame != self.pose_frame:
                    continue
                q = tr.transform.rotation
                yaw = math.atan2(2.0 * (q.w * q.z + q.x * q.y),
                                 1.0 - 2.0 * (q.y * q.y + q.z * q.z))
                return np.array([tr.transform.translation.x,
                                 tr.transform.translation.y, yaw])
        return None

    # ── phase 1: azimuth ────────────────────────────────────────────────
    def calibrate_step(self, pose, pts, w):
        """Turn one keyframe into a motion pair for the hand-eye solve."""
        self.pending.append((pose, pts, w))
        prev = self.prev
        self.prev = (pose, pts, w)
        if prev is None:
            return

        # Identity init, deliberately: seeding this from odometry would fold
        # the answer into its own measurement. Keyframe motion is bounded, so
        # the gate covers it unaided.
        A, k, rms = icp(pts, prev[1], w, np.zeros(3), self.track_gate,
                        self.min_span)
        if A is None or rms > self.pair_rms_max:
            return
        B = compose(invert(prev[0]), pose)
        # theta_a == theta_b is forced by the structure of X·A = B·X, so a pair
        # that breaks it is a mismatch, not a measurement.
        if abs(wrap(A[2] - B[2])) > self.rot_tol:
            return

        # And so is |t_a| == |(R_b - I)·t_x + t_b|, because R(psi) preserves
        # length. That is computable from odometry alone — the camera is bolted
        # at the URDF offset, so t_x is known to a few cm well before psi is —
        # which makes it a gate on the input rather than a check on the output.
        #
        # It matters here in a way it would not on a dense ceiling. With only
        # two landmarks in view the rigid fit is exactly determined, so `rms`
        # is near zero whether the correspondence is right or wrong and
        # pair_rms_max can never fire. Lights keep entering and leaving frame,
        # so the "same" pair between keyframes is often not the same two
        # physical lights; ICP still returns a clean-looking transform and the
        # rotation gate passes it, because what the bad match corrupts is the
        # translation. Measured on this ceiling, accepted pairs ran |t_a|/|t_b|
        # from 0.40 to 1.65 where they should sit near 1.0, and it was those
        # pairs that drove the hand-eye residual to 214 mm.
        c, s = math.cos(B[2]), math.sin(B[2])
        rot_minus_i = np.array([[c - 1.0, -s], [s, c - 1.0]])
        expect = float(np.linalg.norm(rot_minus_i @ self.camera_xy + B[:2]))
        if abs(float(np.hypot(A[0], A[1])) - expect) > self.max_t_mismatch:
            self.rej_translation += 1
            return

        self.pairs.append((A, B))
        self.excitation += abs(B[2])
        if len(self.pairs) % 5 == 0:
            self.get_logger().info(
                f'  {len(self.pairs)}/{self.min_pairs} motion pairs '
                f'(rotation {self.excitation:.1f} rad)'
                + (f', {self.rej_translation} rejected on translation'
                   if self.rej_translation else ''))
        if self.solve_at is None:
            self.solve_at = self.min_pairs
        if len(self.pairs) >= self.solve_at:
            self.lock_azimuth()

    def lock_azimuth(self):
        urdf = np.array([float(v) for v in
                         self.get_parameter('camera_xy').value])
        # Both solves, always. The fixed-lever one is the answer, because it is
        # the well-conditioned question; the free one is kept purely as a
        # cross-check, since a lever arm that comes back far from where the
        # camera is bolted is the clearest single sign that the pairs are bad.
        free_psi, free_t, free_resid = solve_hand_eye(self.pairs)
        if self.estimate_lever:
            psi, t_x, resid = free_psi, free_t, free_resid
        else:
            psi, t_x, resid = solve_hand_eye_fixed_lever(self.pairs, urdf)
            self.get_logger().info(
                f'azimuth from the fixed-lever solve: {math.degrees(psi):+.2f} '
                f'deg, residual {resid * 1000:.1f} mm  '
                f'(free-lever cross-check: {math.degrees(free_psi):+.2f} deg, '
                f'residual {free_resid * 1000:.1f} mm, lever '
                f'[{free_t[0]:+.3f} {free_t[1]:+.3f}])')

        # Check before committing. A rejected solve keeps collecting rather
        # than locking in a wrong mount transform, because everything after
        # this point -- where each landmark is written, whether repeat
        # sightings of one light land on top of each other -- is built on it.
        resid_mm = resid * 1000.0
        lever_err = float(np.linalg.norm(free_t - urdf))
        bad = []
        if resid_mm > self.max_resid_mm:
            bad.append(f'residual {resid_mm:.1f} mm exceeds '
                       f'{self.max_resid_mm:.0f} mm — the solve does not fit '
                       f'its own data')
        # The free-lever estimate is a warning, not a rejection. It is the
        # weakly observed part of the problem — its columns scale with the
        # per-pair rotation — so it drifts even on pairs good enough to give a
        # sound azimuth. It is still worth hearing: the camera is bolted, so a
        # large disagreement says the pairs are suspect even when the residual
        # happens to look acceptable.
        if (self.excitation >= self.lever_exc
                and lever_err > self.max_lever_err and not self.estimate_lever):
            self.get_logger().warn(
                f'free-lever cross-check is {lever_err:.2f} m from the URDF '
                f'[{urdf[0]:+.3f} {urdf[1]:+.3f}] — the azimuth below uses the '
                f'bolted value and may still be sound, but treat it with '
                f'suspicion and check the landmarks')
        elif (self.estimate_lever and self.excitation >= self.lever_exc
              and lever_err > self.max_lever_err):
            bad.append(f'lever arm [{t_x[0]:+.3f} {t_x[1]:+.3f}] is '
                       f'{lever_err:.2f} m from the URDF [{urdf[0]:+.3f} '
                       f'{urdf[1]:+.3f}], but the camera is bolted there')
        if bad:
            self.solve_at = len(self.pairs) + 10
            self.get_logger().error(
                'rejecting the mount solve from %d pairs:\n  %s\n'
                '  NOT locking an azimuth — retrying at %d pairs.\n'
                '  Almost always this is the landmarks, not the driving: with '
                'only 2-3 lights in view one bad detection has nothing to '
                'outvote it. Check `ros2 topic echo '
                '/ceiling_features/lights --no-arr` and the debug image.'
                % (len(self.pairs), '\n  '.join(bad), self.solve_at))
            return

        if self.excitation >= self.lever_exc:
            lever, self.lever_source = t_x, 'estimated'
        else:
            lever, self.lever_source = urdf, 'urdf'
        self.X = np.array([lever[0], lever[1], psi])
        self.hand_eye = dict(azimuth_deg=math.degrees(psi),
                             lever_arm_m=[float(lever[0]), float(lever[1])],
                             lever_arm_estimated=[float(t_x[0]), float(t_x[1])],
                             lever_source=self.lever_source,
                             residual_mm=resid * 1000.0,
                             pairs=len(self.pairs),
                             excitation_rad=self.excitation)

        self.get_logger().info(
            f'\nceiling azimuth: {math.degrees(psi):+.2f} deg   '
            f'(from {len(self.pairs)} pairs, residual {resid * 1000:.1f} mm)\n'
            f'  0 deg = the ceiling basis e1 points to the robot front, '
            f'+90 deg = to its left\n'
            f'  lever arm : [{lever[0]:+.4f} {lever[1]:+.4f}] m  '
            f'({self.lever_source}; URDF says '
            f'[{urdf[0]:+.4f} {urdf[1]:+.4f}], solve says '
            f'[{t_x[0]:+.4f} {t_x[1]:+.4f}])\n'
            f'  this is the yaw the mount TF should carry — compare it with '
            f'the --yaw now in the launch scripts\n'
            f'  for the full mount TF: ros2 run argo_mini ceiling_calibrate '
            f'--ros-args -p ceiling_azimuth_deg:={math.degrees(psi):.4f}')
        if self.lever_source == 'urdf':
            self.get_logger().warn(
                f'only {self.excitation:.1f} rad of rotation in the drive, so '
                'the lever arm is not observable — keeping the URDF value. '
                'Drive some turns to measure it.')

        held, self.pending = self.pending, []
        for pose, pts, w in held:
            self.insert(pose, pts, w)
        self.get_logger().info(
            f'replayed {len(held)} calibration keyframes into the map')
        self.publish_map()

    # ── phase 2: map ────────────────────────────────────────────────────
    def insert(self, pose, pts, w):
        """Fold one keyframe of landmarks into the map."""
        stamp = time.time()
        base = xform(self.X, pts)            # ceiling frame -> base_link
        known = self.map_points()

        used, matched, rms = pose, 0, float('inf')
        if len(known) >= 2:
            # Start from the laser pose but let the ceiling correct it: the
            # ceiling is the rigid structure, that is the entire premise.
            fit, matched, rms = icp(base, known, w, pose, self.assoc_gate,
                                    self.min_span)
            if fit is not None:
                used = self.clamp(fit, pose)

        obs = xform(used, base)
        # Only extend the map from a pose the map itself agrees with. A pose
        # resting on the laser alone is exactly the drifting thing the ceiling
        # is meant to fix, and landmarks planted on it become ghosts.
        trusted = len(known) == 0 or (matched >= 2 and rms < self.assoc_gate)
        for p, wi in zip(obs, w):
            if len(known):
                d = np.linalg.norm(known - p, axis=1)
                j = int(d.argmin())
                if d[j] < self.assoc_gate:
                    self.marks[j].add(p[0], p[1], wi, stamp)
                    continue
            if trusted:
                m = Landmark()
                m.add(p[0], p[1], wi, stamp)
                self.marks.append(m)
                known = self.map_points()

    def clamp(self, fit, prior):
        """Bound the ceiling's correction to the laser pose.

        Unbounded, small per-keyframe corrections compound and the ceiling map
        slowly walks out of the laser map frame — at which point it no longer
        describes the same room as the stored table waypoints, which is the
        one thing it must not do. A correction at the limit is a disagreement
        worth hearing about, not something to absorb quietly.
        """
        d = fit - prior
        d[2] = wrap(d[2])
        n = float(np.linalg.norm(d[:2]))
        if n <= self.max_anchor_xy and abs(d[2]) <= self.max_anchor_yaw:
            self.correction = d
            return fit
        self.get_logger().warn(
            f'ceiling disagrees with the laser pose by {n:.2f} m / '
            f'{math.degrees(d[2]):+.1f} deg — clamping', throttle_duration_sec=5.0)
        if n > self.max_anchor_xy:
            d[:2] *= self.max_anchor_xy / n
        d[2] = max(-self.max_anchor_yaw, min(self.max_anchor_yaw, d[2]))
        self.correction = d
        out = prior + d
        out[2] = wrap(out[2])
        return out

    def map_points(self):
        return np.array([m.xy for m in self.marks], float).reshape(
            len(self.marks), 2)

    def confirmed(self):
        return [m for m in self.marks if m.n >= self.min_obs]

    # ── output ──────────────────────────────────────────────────────────
    def publish_map(self):
        msg = PointCloud()
        msg.header.stamp = self.get_clock().now().to_msg()
        msg.header.frame_id = self.pose_frame or self.map_frame
        z = self.camera_z + (self.height_sum / self.height_n
                             if self.height_n else 0.0)
        n_ch, s_ch = ChannelFloat32(name='count'), ChannelFloat32(name='sigma_mm')
        for m in self.confirmed():
            p = m.xy
            msg.points.append(Point32(x=float(p[0]), y=float(p[1]), z=float(z)))
            n_ch.values.append(float(m.n))
            s_ch.values.append(float(m.sigma * 1000.0))
        msg.channels = [n_ch, s_ch]
        self.pub_map.publish(msg)

    def save(self):
        keep = self.confirmed()
        if not keep:
            return
        doc = {
            'frame_id': self.pose_frame or self.map_frame,
            'registered_to_laser_map': self.pose_frame == self.map_frame,
            'created': time.strftime('%Y-%m-%dT%H:%M:%S'),
            'keyframes': self.frames,
            'ceiling_height_above_camera_m':
                round(self.height_sum / self.height_n, 4) if self.height_n else None,
            'mount': self.hand_eye or {
                'azimuth_deg': math.degrees(self.X[2]) if self.X is not None else None,
                'lever_arm_m': [float(self.X[0]), float(self.X[1])]
                               if self.X is not None else None,
                'lever_source': self.lever_source,
                'source': 'parameter'},
            'landmarks': [{'x': round(float(m.xy[0]), 4),
                           'y': round(float(m.xy[1]), 4),
                           'n': m.n,
                           'sigma_mm': round(m.sigma * 1000.0, 1)} for m in keep],
        }
        os.makedirs(os.path.dirname(self.map_path) or '.', exist_ok=True)
        tmp = self.map_path + '.tmp'
        with open(tmp, 'w') as f:
            json.dump(doc, f, indent=2)
        os.replace(tmp, self.map_path)
        prov = len(self.marks) - len(keep)
        self.get_logger().info(
            f'saved {len(keep)} landmarks to {self.map_path}'
            + (f' ({prov} still provisional, under {self.min_obs} sightings)'
               if prov else ''))

    def load(self):
        if not os.path.exists(self.map_path):
            self.get_logger().info(f'no map at {self.map_path} — starting fresh')
            return
        with open(self.map_path) as f:
            doc = json.load(f)
        for d in doc.get('landmarks', []):
            m = Landmark()
            # Re-enter the stored mean as its own observation history so it
            # carries its earned weight and is not immediately dragged off by
            # one new sighting. The stored spread is put back too, split evenly
            # across the axes — only the total was saved — otherwise a landmark
            # would come back looking perfectly certain and the sigma written
            # out next time would be a fiction.
            w = float(d.get('n', 1))
            m.add(d['x'], d['y'], w, 0.0)
            var = (float(d.get('sigma_mm', 0.0)) / 1000.0) ** 2 / 2.0
            m.sxx += w * var
            m.syy += w * var
            m.n = int(d.get('n', 1))
            self.marks.append(m)
        mount = doc.get('mount') or {}
        if self.X is None and mount.get('azimuth_deg') is not None:
            lever = mount.get('lever_arm_m') or \
                [float(v) for v in self.get_parameter('camera_xy').value]
            self.X = np.array([lever[0], lever[1],
                               math.radians(mount['azimuth_deg'])])
            self.lever_source = mount.get('lever_source', 'stored')
            self.get_logger().info(
                f'loaded azimuth {mount["azimuth_deg"]:+.2f} deg from the map')
        self.get_logger().info(
            f'loaded {len(self.marks)} landmarks from {self.map_path}')


def main(args=None):
    rclpy.init(args=args)
    node = CeilingMapBuilder()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.save()
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == '__main__':
    main()
