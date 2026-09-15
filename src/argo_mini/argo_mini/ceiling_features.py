#!/usr/bin/env python3
"""Ceiling landmark detector for the upward-facing HP60C.

The restaurant floor is not rigid — chairs, tables and people move hourly — so
the laser map drifts against reality. The ceiling is the one rigid structure in
the building, which is what makes it worth localising against.

This node finds the landmarks. It does *not* localise; that is the job of the
node downstream of it.

Two channels, deliberately split by what each sensor is actually good at:

  depth  -> the ceiling *plane*. Measured at 13.8 mm RMS over 3.6 m, so the
            plane fit is excellent, and re-fitting it every frame means the
            mount tilt never has to be entered by hand.
  rgb    -> the *landmarks*. Lights are flat against the ceiling and sprinkler
            pipes are ~25 mm across at 3.6 m, so neither shows up as usable
            geometry in depth. In RGB the lights blaze and the pipes are high
            contrast. Depth is also capped at 4.095 m, which the tilted far half
            of the frame exceeds, so landmarks must not depend on it.

Depth is already registered to the colour image by the SDK (both streams
publish fx=571 and frame ascamera_hp60c_color_0), so depth pixel (u,v) and rgb
pixel (u,v) are the same ray and no cross-calibration is needed.

The mount is tilted ~24 deg off vertical and that is fixed in hardware, so it
is a permanent parameter of the system rather than something to correct. Two
consequences are designed around here:

  * Incidence on the ceiling sweeps ~1 deg to ~47 deg across the frame, so
    landmark quality is strongly position-dependent: 6.3 mm/px at the near edge
    against 9.2 mm/px at the far edge, and depth dies entirely past ~29 deg
    (3.59 m / cos(29 deg) hits the 4.095 m cap). Every landmark is therefore
    published with its incidence angle so the consumer can weight it instead of
    treating all detections as equally good.
  * Anything standing off below the ceiling is reported in the wrong place,
    by drop * tan(incidence), and the error moves as the robot drives. Lights
    here measured 16 mm off the slab -- flush within the plane noise -- so they
    stay accurate to ~17 mm at worst. Sprinkler pipes are standoff-mounted and
    would swing 5-16 cm, so pipes are for orientation, not metric position.

Output is metric: every landmark is reported in metres on the ceiling plane, in
a basis rigidly tied to the camera, so the consumer only has to match points.
"""

import time
from typing import NamedTuple

import numpy as np
import rclpy
from rclpy.node import Node
from rclpy.qos import QoSPresetProfiles
from sensor_msgs.msg import CameraInfo, Image
from geometry_msgs.msg import Point32, Vector3Stamped
from sensor_msgs.msg import ChannelFloat32, PointCloud

try:
    import cv2
except ImportError:                                    # pragma: no cover
    raise SystemExit('ceiling_features needs opencv (python3-opencv)')

DEPTH_MAX = 4.09          # m: the HP60C saturates at 4095 mm, drop that bin


class Landmark(NamedTuple):
    """One detection, in pixels and in metres on the ceiling plane."""
    u: float
    v: float
    area: int
    x: float
    y: float
    rng: float
    inc: float


class CeilingFeatures(Node):
    def __init__(self):
        super().__init__('ceiling_features')

        self.declare_parameter('rgb_topic',
                               '/ascamera_hp60c/camera_publisher/rgb0/image')
        self.declare_parameter('depth_topic',
                               '/ascamera_hp60c/camera_publisher/depth0/image_raw')
        self.declare_parameter('info_topic',
                               '/ascamera_hp60c/camera_publisher/rgb0/camera_info')
        # Light blobs: area in pixels at ~3.6 m. A 0.3 m fixture is ~48 px
        # across, so ~1800 px of area.
        #
        # The floor is 500 rather than the 60 first guessed, because two kinds
        # of bright thing on this ceiling are not landmarks and both were being
        # promoted to one:
        #
        #   glints     specular highlights on a fixture's own mounting bracket
        #              and on the conduit. Measured 9-93 px.
        #   glow pools light cast onto the slab by a fixture out of frame. No
        #              housing, no hard edge. Measured 317 px.
        #
        # Neither is fixed to the ceiling the way a fixture is — a glow pool in
        # particular slides as the robot drives — so both corrupt a pose solve
        # rather than helping it, and with only 2 or 3 real lights in view there
        # is no redundancy to absorb them. Measured over 8 live frames, real
        # fixtures ran 1522-2757 px against a largest non-fixture of 93 px, so
        # 500 sits with 3x margin below the smallest real one.
        #
        # This is keyed to ~0.3 m fixtures. A genuinely small fixture (~0.2 m)
        # at the far edge of the frame would land near 380 px and be rejected;
        # if a ceiling like that turns up, lower this and find another way to
        # drop glow pools.
        self.declare_parameter('light_min_area', 500)
        self.declare_parameter('light_max_area', 20000)
        # Auto-exposure keeps the frame off the rail — a lit fixture measured
        # only 247/255 — so an absolute threshold finds nothing. Threshold
        # relative to the brightest thing in frame instead, with a floor so a
        # ceiling with no light in view does not promote noise to a landmark.
        # 0.75 rather than 0.90: keyed to the frame maximum, a high fraction
        # lets one bright fixture suppress dimmer ones, so which landmarks
        # exist would change frame to frame — worse for matching than a few
        # extra candidates.
        self.declare_parameter('light_threshold_floor', 150)
        self.declare_parameter('light_threshold_frac', 0.75)
        self.declare_parameter('centroid_radius_px', 26)
        # A fixture is compact; window glare and lit walls are elongated and
        # fill their bounding box loosely.
        self.declare_parameter('light_max_aspect', 2.0)
        self.declare_parameter('light_min_fill', 0.5)
        # Past this incidence the ceiling is ~9 mm/px, depth is long gone, and
        # any standoff error is multiplied by tan(). Drop those detections
        # rather than let the consumer weigh them.
        self.declare_parameter('max_incidence_deg', 45.0)
        # The mount is rigid and the tilt is fixed in hardware, so the plane
        # genuinely is near-constant and can be smoothed hard. It is worth
        # doing: the per-frame fit wobbles ~1 deg, and because every landmark
        # is projected through that same plane the wobble enters as a
        # common-mode ~10 mm shift of the whole landmark set. Smoothing trades
        # response to floor slope — which changes slowly — for that.
        self.declare_parameter('plane_alpha', 0.02)
        # The plane fit is the expensive half of this node (~100 ms against
        # ~11 ms for the whole landmark path) and both run on one executor, so
        # fitting on every depth frame starves the landmarks: measured, the node
        # published 1.4 Hz while the camera was offering 6.3 Hz of RGB. Nothing
        # is lost by slowing it down — the mount is rigid, the plane is smoothed
        # at plane_alpha anyway, and what it tracks (floor slope under the
        # robot) changes over metres of driving, not between frames.
        self.declare_parameter('plane_rate_hz', 2.0)
        # Lines: conduit runs and slab seams. These matter because a ceiling of
        # lights is rotationally ambiguous — a regular grid maps onto itself
        # under 90 deg — and because on a sparse ceiling there may only be two
        # lights in view, which pins position but leaves rotation the weakest
        # part of the solve. A conduit run crosses the whole frame and is there
        # in every frame, so it is exactly the missing constraint.
        #
        # Orientation only, never metric position: pipes are standoff-mounted,
        # so drop * tan(incidence) puts them 5-16 cm from where they look, and
        # that error moves as the robot drives. An angle is unaffected.
        self.declare_parameter('publish_lines', True)
        self.declare_parameter('line_canny_lo', 40)
        self.declare_parameter('line_canny_hi', 120)
        self.declare_parameter('line_hough_threshold', 60)
        self.declare_parameter('line_min_length_px', 70)
        self.declare_parameter('line_max_gap_px', 12)
        # A pixel much nearer than the ceiling plane along the same ray is not
        # the ceiling — it is a wall, a doorframe, a person. Depth dies past
        # 4.095 m so much of the far ceiling has none, and where it is missing
        # this test simply abstains and incidence does the gating.
        self.declare_parameter('line_near_frac', 0.85)
        self.declare_parameter('publish_debug', True)

        self.p_min_area = int(self.get_parameter('light_min_area').value)
        self.p_max_area = int(self.get_parameter('light_max_area').value)
        self.p_floor = int(self.get_parameter('light_threshold_floor').value)
        self.p_crad = int(self.get_parameter('centroid_radius_px').value)
        self.p_frac = float(self.get_parameter('light_threshold_frac').value)
        self.p_aspect = float(self.get_parameter('light_max_aspect').value)
        self.p_fill = float(self.get_parameter('light_min_fill').value)
        self.p_max_inc = float(self.get_parameter('max_incidence_deg').value)
        self.p_alpha = float(self.get_parameter('plane_alpha').value)
        rate = float(self.get_parameter('plane_rate_hz').value)
        self.p_plane_period = (1.0 / rate) if rate > 0.0 else 0.0
        self.do_lines = bool(self.get_parameter('publish_lines').value)
        self.p_canny = (int(self.get_parameter('line_canny_lo').value),
                        int(self.get_parameter('line_canny_hi').value))
        self.p_hough = int(self.get_parameter('line_hough_threshold').value)
        self.p_minlen = int(self.get_parameter('line_min_length_px').value)
        self.p_maxgap = int(self.get_parameter('line_max_gap_px').value)
        self.p_near = float(self.get_parameter('line_near_frac').value)
        # The frame is dim and low contrast and the tilt makes the near edge
        # much brighter than the far edge, so a global stretch would wash out
        # one end. Tiled equalisation keeps both usable.
        self.clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8))
        self.debug = bool(self.get_parameter('publish_debug').value)

        self.K = None            # fx, fy, cx, cy once camera_info arrives
        self.plane = None        # (normal, offset) smoothed, camera optical frame
        self.plane_n = 0         # fits folded in so far
        self.last_plane_t = None  # monotonic seconds, for plane_rate_hz
        self.depth = None
        self.count_n = self.count_sum = 0      # 10 s window of landmark counts
        self.count_min = 10**9
        self.last_count_t = 0.0

        q = QoSPresetProfiles.SENSOR_DATA.value
        self.create_subscription(
            CameraInfo, self.get_parameter('info_topic').value, self.on_info, q)
        self.create_subscription(
            Image, self.get_parameter('depth_topic').value, self.on_depth, q)
        self.create_subscription(
            Image, self.get_parameter('rgb_topic').value, self.on_rgb, q)

        # PointCloud (not PointCloud2) keeps this readable in rviz and trivial
        # to consume: one point per landmark, in metres on the ceiling plane.
        self.pub_lights = self.create_publisher(PointCloud, '~/lights', 10)
        self.pub_lines = (self.create_publisher(Vector3Stamped, '~/orientation', 10)
                          if self.do_lines else None)
        self.pub_debug = (self.create_publisher(Image, '~/debug_image', 1)
                          if self.debug else None)

        self.get_logger().info('ceiling_features up — waiting for camera_info')

    # ── inputs ──────────────────────────────────────────────────────────
    def on_info(self, msg):
        if self.K is None:
            self.K = (msg.k[0], msg.k[4], msg.k[2], msg.k[5])
            self.get_logger().info(
                f'intrinsics fx={self.K[0]:.1f} fy={self.K[1]:.1f} '
                f'cx={self.K[2]:.1f} cy={self.K[3]:.1f}')

    def on_depth(self, msg):
        if msg.encoding not in ('16UC1', 'mono16'):
            self.get_logger().warn(f'unexpected depth encoding {msg.encoding}',
                                   once=True)
            return
        d = np.frombuffer(msg.data, np.uint16).reshape(
            msg.height, msg.width).astype(np.float32) / 1000.0
        self.depth = d
        # Startup is the exception: the running mean needs its first handful of
        # fits promptly, because until the plane settles every landmark is being
        # projected through a plane that is still moving.
        now = time.monotonic()
        if (self.plane_n < 10 or self.last_plane_t is None
                or now - self.last_plane_t >= self.p_plane_period):
            self.last_plane_t = now
            self.update_plane(d)

    def on_rgb(self, msg):
        if self.K is None or self.plane is None:
            return
        if msg.encoding == 'rgb8':
            bgr = np.frombuffer(msg.data, np.uint8).reshape(
                msg.height, msg.width, 3)[:, :, ::-1]
        elif msg.encoding == 'bgr8':
            bgr = np.frombuffer(msg.data, np.uint8).reshape(
                msg.height, msg.width, 3)
        else:
            self.get_logger().warn(f'unexpected rgb encoding {msg.encoding}',
                                   once=True)
            return

        marks = self.to_ceiling(self.detect_lights(bgr))
        self.publish_lights(marks, msg.header)
        self.report_count(len(marks))
        lines = self.detect_lines(bgr) if self.do_lines else None
        if lines is not None:
            self.publish_lines(lines, msg.header)
        if self.pub_debug is not None:
            self.publish_debug(bgr, marks, lines, msg.header)

    # ── lines: conduit runs and slab seams ──────────────────────────────
    def project(self, u, v, e1, e2, n, off):
        """One pixel -> (x, y) on the ceiling plane, or None if it isn't ceiling.

        Two gates, in the order that costs least. Incidence first, because past
        the limit the ceiling is ~9 mm/px and a standoff error is multiplied by
        tan(). Then depth: if this ray has a valid return much nearer than where
        it would meet the ceiling plane, something solid is in the way and this
        is a wall or a doorframe, not the slab. Where depth is missing — which
        is most of the far half of the frame, past the 4.095 m cap — the test
        abstains rather than rejecting, so the far ceiling is not thrown away.
        """
        r = np.array([(u - self.K[2]) / self.K[0],
                      (v - self.K[3]) / self.K[1], 1.0])
        rn = r / np.linalg.norm(r)
        cos_inc = float(n @ rn)
        if cos_inc <= 1e-6:
            return None
        if np.degrees(np.arccos(min(1.0, cos_inc))) > self.p_max_inc:
            return None
        p = r * (off / (n @ r))
        if self.depth is not None:
            iv, iu = int(round(v)), int(round(u))
            if 0 <= iv < self.depth.shape[0] and 0 <= iu < self.depth.shape[1]:
                d = float(self.depth[iv, iu])
                if 0.05 < d < DEPTH_MAX and d < self.p_near * float(p[2]):
                    return None
        return float(p @ e1), float(p @ e2)

    def detect_lines(self, bgr):
        """Length-weighted dominant orientation of the ceiling's linear features.

        Segments are projected onto the ceiling plane before their angle is
        taken. Measuring the angle in pixels would be wrong: the mount is tilted
        ~24 deg, so perspective turns a single straight conduit run into
        different pixel angles depending on where it falls in frame.

        Orientations are mod 180 deg — a line has no head or tail — so they are
        averaged as doubled angles. Averaging them directly would put the mean
        of 179 deg and 1 deg at 90 deg, exactly wrong.
        """
        if self.plane is None or self.K is None:
            return None
        g = self.clahe.apply(cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY))
        g = cv2.GaussianBlur(g, (5, 5), 0)
        edges = cv2.Canny(g, self.p_canny[0], self.p_canny[1])
        segs = cv2.HoughLinesP(edges, 1, np.pi / 360, threshold=self.p_hough,
                               minLineLength=self.p_minlen,
                               maxLineGap=self.p_maxgap)
        if segs is None:
            return None
        e1, e2, n, off = self.plane_basis()

        cs = sn = 0.0
        total = 0.0
        kept = []
        for x1, y1, x2, y2 in segs.reshape(-1, 4):
            a = self.project(x1, y1, e1, e2, n, off)
            b = self.project(x2, y2, e1, e2, n, off)
            if a is None or b is None:
                continue
            dx, dy = b[0] - a[0], b[1] - a[1]
            L = float(np.hypot(dx, dy))
            if L < 1e-3:
                continue
            th = float(np.arctan2(dy, dx))
            cs += L * np.cos(2.0 * th)
            sn += L * np.sin(2.0 * th)
            total += L
            kept.append((x1, y1, x2, y2))
        if total <= 0.0 or not kept:
            return None
        # |resultant| / total length: 1.0 when every segment agrees, ~0 when
        # they are scattered. This is the number that says whether the angle
        # means anything, so it is published alongside rather than hidden.
        conc = float(np.hypot(cs, sn) / total)
        ang = float(np.arctan2(sn, cs) / 2.0) % np.pi
        return ang, conc, len(kept), kept

    def publish_lines(self, res, header):
        ang, conc, n_seg, _ = res
        msg = Vector3Stamped()
        msg.header = header
        msg.header.frame_id = 'ceiling_plane'
        # x: orientation in the ceiling basis, radians, mod pi
        # y: concentration 0..1 — how much the segments agree
        # z: how many segments survived the ceiling gate
        msg.vector.x = ang
        msg.vector.y = conc
        msg.vector.z = float(n_seg)
        self.pub_lines.publish(msg)

    def report_count(self, n_marks):
        """Say out loud how many lights are in view, because it is the single
        number that decides whether anything downstream can work.

        Two landmarks is the bare minimum the pose solve needs, and at exactly
        two it has no redundancy: one bad detection goes straight into the
        answer instead of being outvoted. That failure is silent — the map
        builder still prints a confident azimuth — so the detector says it
        plainly rather than leaving it to be inferred from an empty map.
        """
        self.count_n += 1
        self.count_sum += n_marks
        self.count_min = min(self.count_min, n_marks)
        now = time.monotonic()
        if now - self.last_count_t < 10.0:
            return
        self.last_count_t = now
        avg = self.count_sum / max(1, self.count_n)
        lo = self.count_min
        self.count_n = self.count_sum = 0
        self.count_min = 10**9
        if avg < 1.0:
            self.get_logger().warn(
                f'no lights in view (10 s average {avg:.1f}) — nothing '
                f'downstream can localise from this')
        elif avg < 3.0:
            self.get_logger().warn(
                f'only {avg:.1f} lights in view on average (minimum {lo}) — '
                f'2 is the bare minimum and leaves the pose solve no '
                f'redundancy; 3+ is where it gets reliable')
        else:
            self.get_logger().info(
                f'{avg:.1f} lights in view on average (minimum {lo})')

    # ── ceiling plane ───────────────────────────────────────────────────
    def update_plane(self, d):
        """Re-fit the dominant plane so mount tilt is measured, never entered."""
        if self.K is None:
            return
        fx, fy, cx, cy = self.K
        h, w = d.shape
        # Subsample: the plane is huge and this runs on a Jetson.
        step = 4
        ds = d[::step, ::step]
        u, v = np.meshgrid(np.arange(0, w, step), np.arange(0, h, step))
        ok = (ds > 0.05) & (ds < DEPTH_MAX)
        if ok.sum() < 500:
            return
        pts = np.stack([(u[ok] - cx) / fx * ds[ok],
                        (v[ok] - cy) / fy * ds[ok],
                        ds[ok]], axis=1)

        n, off = self.fit_plane(pts)
        if n is None:
            return
        self.plane_n += 1
        if self.plane is None:
            self.plane = (n, off)
            return
        # Running mean first, exponential after: 1/k converges in a handful of
        # frames, then the floor at p_alpha takes over and smooths hard. A bare
        # exponential at p_alpha would instead spend its whole startup transient
        # publishing landmark positions that are quietly wrong.
        a = max(self.p_alpha, 1.0 / self.plane_n)
        pn, po = self.plane
        n = pn + a * (n - pn)
        n /= np.linalg.norm(n)
        self.plane = (n, po + a * (off - po))
        if self.plane_n == 30:
            tilt = np.degrees(np.arccos(min(1.0, abs(n[2]))))
            self.get_logger().info(
                f'ceiling plane settled: {po:.3f} m, tilt {tilt:.1f} deg')

    @staticmethod
    def _svd_plane(pts):
        """Total-least-squares plane through a point set."""
        c = pts.mean(0)
        _, _, vt = np.linalg.svd(pts - c, full_matrices=False)
        n = vt[2] / np.linalg.norm(vt[2])
        if n[2] < 0:                    # keep the normal pointing at the ceiling
            n = -n
        return n, float(n @ c)

    @staticmethod
    def fit_plane(pts, iters=120, tol=0.05, refine=3, rng=None):
        """RANSAC the dominant plane, then polish it on its own inliers.

        Every hypothesis is scored the same way against the same points, so the
        whole set is one (points x hypotheses) product rather than `iters` trips
        through the interpreter. Measured on the Jetson against live depth this
        is ~16x faster (1680 ms -> ~100 ms for 13k points), which is the
        difference between the RGB path getting a look in and not: the fit runs
        on the same single-threaded executor as the landmark detector.

        The winning hypothesis is only ever as good as the inlier tolerance, and
        polishing it once inherits whatever 50 mm-wide slab that hypothesis
        happened to select. So refit on the inliers with a shrinking band: each
        pass re-centres on a tighter set, and the answer stops depending on which
        triplet won. The floor of 20 mm sits just above the ceiling's own 14 mm
        RMS roughness, so the last pass has dropped the walls and fixtures
        without starting to chase the slab's texture. Measured across seeds on
        one frame this takes the tilt spread to 0.79 deg; frame to frame the
        offset holds to 8.9 mm, which is the plane's documented noise floor.
        """
        m = len(pts)
        if m < 3:
            return None, None
        rng = rng if rng is not None else np.random.default_rng()
        # Sampling with replacement: a degenerate triplet just scores badly and
        # loses, which is cheaper than excluding it.
        idx = rng.integers(0, m, size=(iters, 3))
        a, b, c = pts[idx[:, 0]], pts[idx[:, 1]], pts[idx[:, 2]]
        nv = np.cross(b - a, c - a)
        ln = np.linalg.norm(nv, axis=1)
        good = ln > 1e-6
        if not good.any():
            return None, None
        nv = nv[good] / ln[good, None]
        off = np.einsum('ij,ij->i', nv, a[good])
        # Point the normals away from the camera so `off` is a distance. The
        # inlier count is invariant to this, but it keeps the sign convention
        # the same one the refinement and every consumer downstream expects.
        flip = off < 0
        nv[flip] *= -1.0
        off[flip] *= -1.0
        cnt = (np.abs(pts @ nv.T - off) < tol).sum(axis=0)
        k = int(np.argmax(cnt))
        if cnt[k] < 300:
            return None, None

        n, o = nv[k], off[k]
        t = tol
        for _ in range(refine):
            inl = np.abs(pts @ n - o) < t
            if inl.sum() < 300:
                break
            n, o = CeilingFeatures._svd_plane(pts[inl])
            t = max(0.02, t * 0.6)
        return n, o

    def plane_basis(self):
        """Orthonormal basis on the ceiling plane, rigid w.r.t. the camera.

        e1 is the camera's optical X projected onto the plane. The mount is
        rigid and the plane normal is fixed in camera axes, so this basis does
        not rotate with the robot — a landmark's (x, y) is directly comparable
        between frames and between runs.
        """
        n, off = self.plane
        e1 = np.array([1.0, 0.0, 0.0]) - n[0] * n
        e1 /= np.linalg.norm(e1)
        return e1, np.cross(n, e1), n, off

    def to_ceiling(self, uv):
        """Detections -> metres on the ceiling plane. Depth is not consulted.

        Returns a Landmark per surviving detection, keeping the source pixel so
        callers can line a landmark up with the blob it came from. Incidence is
        carried
        through because with a fixed 24 deg mount it is the single best
        predictor of how much a detection can be trusted.
        """
        fx, fy, cx, cy = self.K
        e1, e2, n, off = self.plane_basis()
        out = []
        for u, v, area in uv:
            r = np.array([(u - cx) / fx, (v - cy) / fy, 1.0])
            rn = r / np.linalg.norm(r)
            cos_inc = float(n @ rn)
            if cos_inc <= 1e-6:          # ray parallel to / behind the ceiling
                continue
            inc = float(np.degrees(np.arccos(min(1.0, cos_inc))))
            if inc > self.p_max_inc:
                continue
            p = r * (off / (n @ r))
            out.append(Landmark(u=u, v=v, area=area,
                                x=float(p @ e1), y=float(p @ e2),
                                rng=float(np.linalg.norm(p)), inc=inc))
        return out

    # ── landmarks ───────────────────────────────────────────────────────
    def detect_lights(self, bgr):
        """Bright, compact blobs. Lights are self-luminous, so they survive
        exposure changes and the loss of depth at the far end of the frame."""
        grey = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
        h, w = grey.shape
        thr = max(self.p_floor, self.p_frac * float(grey.max()))
        _, mask = cv2.threshold(grey, thr, 255, cv2.THRESH_BINARY)
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
        n, _, stats, cent = cv2.connectedComponentsWithStats(mask, 8)

        out = []
        for i in range(1, n):
            area = int(stats[i, cv2.CC_STAT_AREA])
            if not (self.p_min_area <= area <= self.p_max_area):
                continue
            x, y = stats[i, cv2.CC_STAT_LEFT], stats[i, cv2.CC_STAT_TOP]
            bw, bh = stats[i, cv2.CC_STAT_WIDTH], stats[i, cv2.CC_STAT_HEIGHT]
            if max(bw, bh) > self.p_aspect * max(1, min(bw, bh)):
                continue
            if area < self.p_fill * bw * bh:
                continue
            # A blob clipped by the frame edge has a centroid biased inward by
            # however much of it is missing, which would show up downstream as
            # a pose error. Drop it; it will be seen whole a frame or two on.
            if x == 0 or y == 0 or x + bw >= w or y + bh >= h:
                continue
            c = self.weighted_centroid(grey, cent[i][0], cent[i][1])
            if c is None:
                continue
            out.append((c[0], c[1], area))
        return out

    def weighted_centroid(self, grey, cu, cv):
        """Sub-pixel centroid from intensity, not from the threshold mask.

        Where the mask edge falls depends on exposure and on how bright the
        neighbouring fixtures are, and a binary centroid moves with it — 1.9 px
        across the useful threshold range, which is 13 mm on the ceiling. An
        intensity-weighted centroid over the same neighbourhood moves 0.02 px.
        This is the standard star-tracker trick, and a ceiling of lights is the
        same problem: bright compact sources on a dim field.
        """
        h, w = grey.shape
        r = self.p_crad
        y0, y1 = max(0, int(cv - r)), min(h, int(cv + r))
        x0, x1 = max(0, int(cu - r)), min(w, int(cu + r))
        win = grey[y0:y1, x0:x1].astype(np.float32)
        if win.size == 0:
            return None
        # Subtract a local background so a bright ceiling does not drag the
        # centroid toward the window centre, then square to favour the core.
        wgt = np.clip(win - np.percentile(win, 20), 0.0, None) ** 2
        tot = wgt.sum()
        if tot < 1e-6:
            return None
        ys, xs = np.mgrid[y0:y1, x0:x1]
        return (float((xs * wgt).sum() / tot), float((ys * wgt).sum() / tot))

    # ── outputs ─────────────────────────────────────────────────────────
    def publish_lights(self, marks, header):
        msg = PointCloud()
        msg.header = header
        msg.header.frame_id = 'ceiling_plane'
        inc_ch = ChannelFloat32(name='incidence_deg')
        rng_ch = ChannelFloat32(name='range_m')
        for m in marks:
            msg.points.append(Point32(x=m.x, y=m.y, z=0.0))
            inc_ch.values.append(m.inc)
            rng_ch.values.append(m.rng)
        msg.channels = [inc_ch, rng_ch]
        self.pub_lights.publish(msg)

    def publish_debug(self, bgr, marks, lines, header):
        img = bgr.copy()
        if lines is not None:
            for x1, y1, x2, y2 in lines[3]:
                cv2.line(img, (x1, y1), (x2, y2), (255, 128, 0), 1)
        for m in marks:
            cv2.circle(img, (int(m.u), int(m.v)), 14, (0, 0, 255), 2)
            cv2.putText(img, f'{m.x:+.2f},{m.y:+.2f}  {m.inc:.0f}deg',
                        (int(m.u) + 16, int(m.v)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.4, (0, 255, 255), 1)
        n, off = self.plane
        tilt = np.degrees(np.arccos(min(1.0, abs(n[2]))))
        cv2.putText(img, f'ceiling {off:.2f} m  tilt {tilt:.1f} deg  '
                         f'lights {len(marks)}',
                    (8, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 1)
        if lines is not None:
            ang, conc, n_seg, _ = lines
            cv2.putText(img, f'lines {n_seg}  orient {np.degrees(ang):.1f} deg'
                             f'  conc {conc:.2f}',
                        (8, 38), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 128, 0), 1)

        out = Image()
        out.header = header
        out.height, out.width = img.shape[:2]
        out.encoding = 'bgr8'
        out.step = out.width * 3
        out.data = np.ascontiguousarray(img).tobytes()
        self.pub_debug.publish(out)


def main(args=None):
    rclpy.init(args=args)
    node = CeilingFeatures()
    rclpy.spin(node)
    node.destroy_node()
    rclpy.shutdown()


if __name__ == '__main__':
    main()
