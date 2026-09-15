#!/usr/bin/env python3
"""Recover the mount azimuth from map sharpness instead of from motion pairs.

`ceiling_map_builder` gets the azimuth from a 2D hand-eye solve on pairs of
keyframes. That works on a dense ceiling and does not work here. The reason is
structural, not a tuning problem: the solve measures motion **pairwise**, so
each pair leans on whatever handful of landmarks happened to be in view, and on
this restaurant ceiling that is two. With two points the rigid fit is exactly
determined, so its residual is near zero whether the correspondence was right or
wrong, and a bad pair is indistinguishable from a good one until it has already
poisoned the answer. Three drives produced three different azimuths and every
one of them failed its own sanity checks.

The azimuth leaves a much stronger signature than pairwise motion, though.

    If the azimuth is wrong, landmarks smear.

Every sighting of one light is rotated into the map through the mount
transform. Get that rotation wrong and repeat sightings of the same fixture
land in a slightly different place each time — by an amount that grows with
how far the robot has moved between them. Get it right and they stack.

So the azimuth is simply the one that makes the map sharpest, and finding it is
a one-dimensional search over something every observation contributes to at
once. That is exactly the redundancy a sparse ceiling is short of pairwise: two
lights per frame is thin, but two lights across nine hundred frames is not.

Usage — drive normally, as for mapping, then Ctrl-C:

    ros2 run argo_mini ceiling_azimuth_scan

Drive for coverage, not for the calibration dance: this does not care about
straight runs versus turns, only that the same fixtures are seen repeatedly
from *different robot positions*. Revisit places. Translation is what separates
a right azimuth from a wrong one, because the smear is proportional to it.
"""

import json
import math
import os
import sys
import time

import numpy as np
import rclpy
from rclpy.node import Node
from rclpy.qos import QoSPresetProfiles
from sensor_msgs.msg import PointCloud
from tf2_ros import Buffer, TransformListener

from argo_mini.ceiling_map_builder import compose, invert, wrap, xform


class Scan(Node):
    def __init__(self):
        super().__init__('ceiling_azimuth_scan')
        self.declare_parameter('lights_topic', '/ceiling_features/lights')
        self.declare_parameter('map_frame', 'map')
        self.declare_parameter('odom_frame', 'odom')
        self.declare_parameter('base_frame', 'base_link')
        # Odom first, and deliberately — the opposite of what the map builder
        # wants. The builder needs the map frame because its output has to be
        # registered to the laser map and the table waypoints. This scan needs
        # no such thing: it only measures how well sightings of one fixture
        # stack, which depends on *relative* pose over short baselines.
        #
        # For that, odom is the better frame. slam_toolbox's map -> base_link
        # jumps whenever it relocalises, and in a restaurant it relocalises
        # against furniture that moves — which is the whole reason this project
        # exists. Measured on the first real scan, that jitter was ~0.15 m and
        # it dominated everything: sigma 142 mm where the landmarks themselves
        # are good to 2 mm. Odom drifts, but it drifts *smoothly*, and slow
        # drift over a few minutes barely blunts the minimum where a 0.15 m jump
        # destroys it.
        self.declare_parameter('prefer_frame', 'odom')
        self.declare_parameter('camera_xy', [0.2575, 0.0])
        self.declare_parameter('keyframe_dist', 0.20)
        self.declare_parameter('keyframe_rot_deg', 8.0)
        self.declare_parameter('min_lights', 2)
        # Sweep range. The installed TF implies +92.361 deg, but that came from
        # an *assumed* mount azimuth of 0 rather than a measurement, so the scan
        # deliberately covers the whole circle rather than a window around it.
        self.declare_parameter('az_min_deg', -180.0)
        self.declare_parameter('az_max_deg', 180.0)
        self.declare_parameter('az_step_deg', 2.0)
        self.declare_parameter('refine_step_deg', 0.1)
        self.declare_parameter('assoc_gate', 0.30)
        self.declare_parameter('min_observations', 5)
        # Always written. A drive costs minutes of someone's time and the scan
        # is pure post-processing, so there is no good reason for a recording
        # to be thrown away — especially since the first real scan turned out to
        # need re-running in a different frame, and could not be.
        self.declare_parameter('record_path',
                               os.path.expanduser('~/maps/ceiling_scan.json'))

        g = lambda k: self.get_parameter(k).value
        self.prefer = str(g('prefer_frame'))
        self.map_frame, self.odom_frame = g('map_frame'), g('odom_frame')
        self.base_frame = g('base_frame')
        self.cam = np.array([float(v) for v in g('camera_xy')])
        self.kf_dist = float(g('keyframe_dist'))
        self.kf_rot = math.radians(float(g('keyframe_rot_deg')))
        self.min_lights = int(g('min_lights'))
        self.gate = float(g('assoc_gate'))
        self.min_obs = int(g('min_observations'))
        self.record_path = str(g('record_path'))

        self.tf = Buffer()
        self.lis = TransformListener(self.tf, self)
        self.frames = []          # (pose, pts) per keyframe
        self.last_kf = None
        self.pose_frame = None
        self.skipped = 0

        self.create_subscription(PointCloud, g('lights_topic'), self.on_lights,
                                 QoSPresetProfiles.SENSOR_DATA.value)
        self.get_logger().info(
            'recording — drive for coverage and revisit places, then Ctrl-C')

    # ── recording ───────────────────────────────────────────────────────
    def robot_pose(self, stamp):
        order = ((self.odom_frame, self.map_frame)
                 if self.prefer == 'odom' else (self.map_frame, self.odom_frame))
        for frame in order:
            try:
                tr = self.tf.lookup_transform(
                    frame, self.base_frame, stamp,
                    timeout=rclpy.duration.Duration(seconds=0.05))
            except Exception:
                continue
            if self.pose_frame is None:
                self.pose_frame = frame
                self.get_logger().info(f'using the {frame} frame')
            if frame != self.pose_frame:
                continue
            q = tr.transform.rotation
            return np.array([tr.transform.translation.x,
                             tr.transform.translation.y,
                             math.atan2(2.0 * (q.w * q.z + q.x * q.y),
                                        1.0 - 2.0 * (q.y * q.y + q.z * q.z))])
        return None

    def on_lights(self, msg):
        pts = np.array([[p.x, p.y] for p in msg.points]).reshape(-1, 2)
        if len(pts) < self.min_lights:
            self.skipped += 1
            return
        pose = self.robot_pose(rclpy.time.Time.from_msg(msg.header.stamp))
        if pose is None:
            self.skipped += 1
            return
        if self.last_kf is not None:
            d = compose(invert(self.last_kf), pose)
            if np.hypot(d[0], d[1]) < self.kf_dist and abs(wrap(d[2])) < self.kf_rot:
                return
        self.last_kf = pose
        self.frames.append((pose, pts))
        n = len(self.frames)
        if n % 25 == 0:
            span = self.travel()
            self.get_logger().info(
                f'  {n} keyframes, {span:.1f} m travelled '
                f'({sum(len(p) for _, p in self.frames)} observations)')

    def travel(self):
        if len(self.frames) < 2:
            return 0.0
        p = np.array([f[0][:2] for f in self.frames])
        return float(np.linalg.norm(np.diff(p, axis=0), axis=1).sum())

    # ── the scan ────────────────────────────────────────────────────────
    def world_points(self, psi):
        """Every sighting, mapped into the map frame through this azimuth.

        Deliberately does *not* run ICP to refine each pose the way the builder
        does. ICP would quietly absorb a wrong azimuth by nudging the pose to
        match, which is exactly the signal being measured here. Raw laser pose
        only, so the smear shows.
        """
        X = np.array([self.cam[0], self.cam[1], psi])
        return np.vstack([xform(pose, xform(X, pts))
                          for pose, pts in self.frames])

    @staticmethod
    def leader_centres(P, gate):
        """Cluster with centres that never move once placed.

        A running mean must not be used here. Under a wrong azimuth the
        sightings smear, and a mean-updating cluster then walks: each new point
        drags the centre a little, the centre reaches the next point, and one
        cluster chains across the room swallowing everything. That makes a
        *wrong* azimuth look like it gathered the most evidence — the first
        version of this scan did exactly that and confidently reported an
        azimuth 24 deg from the truth. A fixed centre cannot chain.
        """
        cent = []
        for q in P:
            if cent and np.linalg.norm(np.asarray(cent) - q, axis=1).min() < gate:
                continue
            cent.append(q)
        return np.asarray(cent)

    def score(self, psi):
        """How well does ONE rigid map explain ALL the sightings?

        That is a model-selection question — fit quality against how many
        landmarks you had to invent — so the cost is

            sum of squared residuals + n_landmarks * gate^2

        Smear pushes up the first term; fragmentation pushes up the second.
        Either term alone is gameable: raw residual goes to zero if every
        sighting becomes its own landmark, and landmark count is lowest when
        everything is smeared into one blob.
        """
        P = self.world_points(psi)
        C = self.leader_centres(P, self.gate)
        d = np.linalg.norm(P[:, None, :] - C[None, :, :], axis=2)
        j = d.argmin(1)
        res = d[np.arange(len(P)), j]
        counts = np.bincount(j, minlength=len(C))
        sse = float((res ** 2).sum())
        conf = counts >= self.min_obs
        sigma = (float(np.sqrt((res[np.isin(j, np.where(conf)[0])] ** 2).mean()))
                 if conf.any() else float('inf'))
        return (sse + len(C) * self.gate ** 2, len(C), int(conf.sum()),
                int(counts[conf].sum()) if conf.any() else 0, sigma)

    def run_scan(self):
        if len(self.frames) < 20:
            self.get_logger().error(
                f'only {len(self.frames)} keyframes — drive further; the scan '
                f'needs the same fixtures seen from many positions')
            return
        span = self.travel()
        total_obs = sum(len(p) for _, p in self.frames)
        print(f'\n{len(self.frames)} keyframes, {total_obs} observations, '
              f'{span:.1f} m travelled, frame "{self.pose_frame}"')
        if span < 3.0:
            print('  WARNING: barely moved. The smear from a wrong azimuth '
                  'grows with travel,\n  so a short drive leaves the minimum '
                  'shallow and the answer weak.')

        lo = float(self.get_parameter('az_min_deg').value)
        hi = float(self.get_parameter('az_max_deg').value)
        step = float(self.get_parameter('az_step_deg').value)
        coarse = np.arange(lo, hi + 1e-9, step)
        print(f'\nscanning {len(coarse)} azimuths from {lo:.0f} to {hi:.0f} deg')
        print(f'{"azimuth":>9} {"cost":>10} {"marks":>7} {"confirmed":>10} '
              f'{"sigma":>10}')

        rows = []
        for i, a in enumerate(coarse):
            r = self.score(math.radians(float(a)))
            rows.append((float(a),) + r)
            if i % 10 == 0:
                print(f'{a:8.1f}d {r[0]:10.2f} {r[1]:7d} {r[2]:10d} '
                      f'{1000*r[4] if np.isfinite(r[4]) else float("nan"):8.1f} mm')

        best = min(rows, key=lambda r: r[1])
        print(f'\ncoarse best: {best[0]:+.1f} deg (cost {best[1]:.2f}, '
              f'{best[3]} confirmed landmarks, sigma {1000*best[5]:.1f} mm)')

        fine_step = float(self.get_parameter('refine_step_deg').value)
        fine = np.arange(best[0] - step, best[0] + step + 1e-9, fine_step)
        frows = [(float(a),) + self.score(math.radians(float(a))) for a in fine]
        fbest = min(frows, key=lambda r: r[1])

        print(f'\n{"="*62}')
        print(f'  ceiling azimuth: {fbest[0]:+.3f} deg')
        print(f'    {fbest[4]} observations in {fbest[3]} confirmed landmarks, '
              f'sigma {1000*fbest[5]:.1f} mm')
        print(f'    installed TF implies +92.361 deg '
              f'(difference {abs(fbest[0] - 92.361):.2f} deg)')
        print(f'\n  for the full mount TF:')
        print(f'    ros2 run argo_mini ceiling_calibrate --ros-args '
              f'-p ceiling_azimuth_deg:={fbest[0]:.4f}')
        print(f'\n  then build the map with it:')
        print(f'    ros2 run argo_mini ceiling_map_builder --ros-args \\')
        print(f'      -p map_path:=/abs/path/ceiling_map.json \\')
        print(f'      -p ceiling_azimuth_deg:={fbest[0]:.4f}')
        print(f'{"="*62}')

        # How sharp is the minimum? A flat curve means the drive did not
        # separate the azimuths and the number above is not worth installing.
        # Sharpness alone is not enough, and the first real scan proved it: it
        # reported a clear minimum (6% of azimuths within 5% of best) while
        # sigma was 142 mm and almost every sighting sat in its own landmark.
        # A least-bad answer can look decisive. So judge the fit itself too.
        sc = np.array([r[1] for r in rows], float)
        share = float(np.mean(sc <= 1.05 * sc.min()))
        contrast = float(sc.max() / sc.min()) if sc.min() > 0 else float('inf')
        sigma_mm = 1000.0 * fbest[5] if np.isfinite(fbest[5]) else float('inf')
        print(f'\nquality of the fit, not just of the minimum:')
        print(f'  sigma            {sigma_mm:8.1f} mm   (want < 40; the '
              f'landmarks themselves are good to ~2 mm)')
        print(f'  contrast         {contrast:8.1f}x    (want > 5)')
        print(f'  within 5% of best{100*share:7.0f}%    (want < 25)')
        print(f'  landmarks        {fbest[2]:8d}     of which {fbest[3]} '
              f'confirmed  (want most of them confirmed)')

        bad = []
        if sigma_mm > 40.0:
            bad.append('sightings are not stacking — no azimuth explains this '
                       'data well')
        if contrast < 5.0:
            bad.append('the cost barely varies across azimuths')
        if share > 0.25:
            bad.append('the drive did not separate the azimuths')
        if fbest[2] and fbest[3] < 0.4 * fbest[2]:
            bad.append('most landmarks were seen too few times to confirm')

        if bad:
            print(f'\n  DO NOT INSTALL THIS VALUE:')
            for b in bad:
                print(f'    - {b}')
            print(f'\n  With sigma this high the error is dominated by pose '
                  f'noise, not by the\n  azimuth. Things to try, cheapest '
                  f'first:')
            print(f'    - rescan the saved recording in the other frame '
                  f'(-p prefer_frame:=map)')
            print(f'    - drive again revisiting the same fixtures more often, '
                  f'and more slowly')
            print(f'    - if the laser pose is jumping, the ceiling cannot be '
                  f'calibrated against it')
        else:
            print(f'\n  Sound. Install it.')

        if self.record_path:
            path = os.path.abspath(self.record_path)
            os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
            with open(path, 'w') as f:
                json.dump({'frame_id': self.pose_frame,
                           'camera_xy': self.cam.tolist(),
                           'azimuth_deg': fbest[0],
                           'frames': [{'pose': p.tolist(), 'pts': q.tolist()}
                                      for p, q in self.frames]}, f)
            print(f'\nrecording saved to {path} — rescan offline without '
                  f'driving again')


def main(args=None):
    # Offline rescan of a saved drive, so re-analysis never costs another one:
    #   ros2 run argo_mini ceiling_azimuth_scan --replay ~/maps/ceiling_scan.json
    argv = list(sys.argv[1:] if args is None else args)
    if '--replay' in argv:
        path = argv[argv.index('--replay') + 1]
        with open(path) as f:
            doc = json.load(f)
        rclpy.init(args=[])
        node = Scan()
        node.frames = [(np.array(fr['pose']), np.array(fr['pts']))
                       for fr in doc['frames']]
        node.pose_frame = doc.get('frame_id')
        node.record_path = ''          # do not overwrite the recording
        print(f'replaying {len(node.frames)} keyframes recorded in frame '
              f'"{node.pose_frame}" from {path}')
        try:
            node.run_scan()
        finally:
            node.destroy_node()
            rclpy.shutdown()
        return

    rclpy.init(args=args)
    node = Scan()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        try:
            node.run_scan()
        finally:
            node.destroy_node()
            if rclpy.ok():
                rclpy.shutdown()


if __name__ == '__main__':
    main()
