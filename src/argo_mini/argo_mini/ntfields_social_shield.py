"""
NTFields Social Shield

Replaces the binary depth_safety_shield with a physics-informed soft
velocity field that is aware of both static obstacles and dynamic humans.

Pipeline
--------
  Nav2 velocity_smoother  →  /cmd_vel_smoothed
  [this node]             →  /cmd_vel
  serial_bridge           →  ESP32 motors

Velocity scaling
----------------
  v_out = v_in × S_total(robot_pos, t)

  S_total = S_static(x,y)      — NTFields distance-field component
          × S_social(x,y,t)    — inverse-Gaussian around each detected human
          × S_close(d_depth)   — close-range depth-camera component

  All components are in [0, 1].  S=1 → full speed.  S=0 → stop.

Human detection (2-D LiDAR leg finder)
---------------------------------------
  1. Segment the LiDAR scan into contiguous point clusters.
  2. Keep clusters whose width is 5–25 cm (leg-sized).
  3. Pair clusters within 30–80 cm of each other (person's two legs).
  4. Track person positions with a nearest-neighbour tracker (50-Hz LiDAR).
  5. Emit (x, y) world-frame person positions to the speed field.

No ML needed for detection — the leg signature in 2-D LiDAR is highly
distinctive and the simple geometric rules work reliably in restaurant /
hotel environments where the floor is mostly uncluttered at ankle height.
"""

import math
from collections import deque

import numpy as np
import rclpy
from rclpy.node import Node
from rclpy.qos import QoSPresetProfiles
from geometry_msgs.msg import Twist, TransformStamped
from sensor_msgs.msg import LaserScan, PointCloud2
from sensor_msgs_py import point_cloud2 as pc2
from std_msgs.msg import String
from visualization_msgs.msg import MarkerArray, Marker
import tf2_ros


# ── LiDAR leg detector ────────────────────────────────────────────────────────

class LegDetector:
    """
    Detects human legs in a 2D LaserScan.

    Algorithm
    ---------
    1. Convert polar scan to Cartesian (x, y) in sensor frame.
    2. Cluster consecutive points whose gap < cluster_gap_m.
    3. Keep clusters whose bounding-circle diameter is within [leg_min, leg_max].
    4. Pair clusters within [pair_min, pair_max] distance → candidate person.
    5. Return list of (cx, cy) person centres in sensor frame.
    """

    def __init__(
        self,
        cluster_gap_m: float = 0.10,
        leg_min_m:     float = 0.04,
        leg_max_m:     float = 0.25,
        pair_min_m:    float = 0.10,
        pair_max_m:    float = 0.80,
    ):
        self.cluster_gap = cluster_gap_m
        self.leg_min     = leg_min_m
        self.leg_max     = leg_max_m
        self.pair_min    = pair_min_m
        self.pair_max    = pair_max_m

    def detect(self, scan: LaserScan) -> list[tuple[float, float]]:
        """Return list of (x, y) person centres in the sensor frame."""
        pts = self._to_cartesian(scan)
        if len(pts) == 0:
            return []

        clusters = self._cluster(pts)
        legs     = self._filter_legs(clusters)
        persons  = self._pair_legs(legs)
        return persons

    def _to_cartesian(self, scan: LaserScan) -> np.ndarray:
        angles = (scan.angle_min
                  + np.arange(len(scan.ranges)) * scan.angle_increment)
        r = np.array(scan.ranges, dtype=np.float32)

        valid = (r > scan.range_min) & (r < scan.range_max) & np.isfinite(r)
        r, angles = r[valid], angles[valid]

        x = r * np.cos(angles)
        y = r * np.sin(angles)
        return np.stack([x, y], axis=1)

    def _cluster(self, pts: np.ndarray) -> list[np.ndarray]:
        clusters, cur = [], [pts[0]]
        for p in pts[1:]:
            if np.linalg.norm(p - cur[-1]) < self.cluster_gap:
                cur.append(p)
            else:
                clusters.append(np.array(cur))
                cur = [p]
        clusters.append(np.array(cur))
        return clusters

    def _filter_legs(self, clusters: list[np.ndarray]) -> list[np.ndarray]:
        legs = []
        for c in clusters:
            diam = np.linalg.norm(c.max(0) - c.min(0))
            if self.leg_min <= diam <= self.leg_max:
                legs.append(c)
        return legs

    def _pair_legs(self, legs: list[np.ndarray]) -> list[tuple[float, float]]:
        centres = [c.mean(0) for c in legs]
        used    = [False] * len(centres)
        persons = []
        for i in range(len(centres)):
            if used[i]:
                continue
            for j in range(i + 1, len(centres)):
                if used[j]:
                    continue
                d = np.linalg.norm(centres[i] - centres[j])
                if self.pair_min <= d <= self.pair_max:
                    mid = (centres[i] + centres[j]) * 0.5
                    persons.append((float(mid[0]), float(mid[1])))
                    used[i] = used[j] = True
                    break
        return persons


# ── simple nearest-neighbour tracker ─────────────────────────────────────────

class PersonTracker:
    """
    Keeps track of detected persons across frames.

    Each track is a smoothed (x, y) world-frame position.
    Tracks that are not updated for `timeout_s` seconds are removed.
    """

    def __init__(self, max_assoc_dist: float = 0.5, timeout_s: float = 1.0,
                 smooth: float = 0.6):
        self.max_dist  = max_assoc_dist
        self.timeout   = timeout_s
        self.smooth    = smooth           # exponential smoothing α
        self._tracks:  dict[int, dict] = {}
        self._next_id: int = 0

    def update(
        self,
        detections: list[tuple[float, float]],
        now: float,
    ) -> list[tuple[float, float]]:
        """
        Associate detections with existing tracks.
        Returns smoothed world-frame positions of all active tracks.
        """
        unmatched = list(range(len(detections)))
        for tid, tr in list(self._tracks.items()):
            best_d, best_i = float('inf'), -1
            for i in unmatched:
                d = math.hypot(detections[i][0] - tr['x'],
                               detections[i][1] - tr['y'])
                if d < best_d:
                    best_d, best_i = d, i
            if best_i >= 0 and best_d < self.max_dist:
                dx, dy = detections[best_i]
                tr['x'] = self.smooth * tr['x'] + (1 - self.smooth) * dx
                tr['y'] = self.smooth * tr['y'] + (1 - self.smooth) * dy
                tr['t'] = now
                unmatched.remove(best_i)

        for i in unmatched:
            self._tracks[self._next_id] = {
                'x': detections[i][0], 'y': detections[i][1], 't': now
            }
            self._next_id += 1

        # Prune stale tracks
        self._tracks = {k: v for k, v in self._tracks.items()
                        if now - v['t'] < self.timeout}

        return [(tr['x'], tr['y']) for tr in self._tracks.values()]


# ── main node ─────────────────────────────────────────────────────────────────

class NTFieldsSocialShield(Node):
    """
    Soft velocity shield with human awareness.

    Replaces depth_safety_shield in the launch file.
    Drop-in replacement: same topics in/out.
    """

    def __init__(self):
        super().__init__('ntfields_social_shield')

        # ── parameters ────────────────────────────────────────────────────
        self.declare_parameter('input_topic',       '/cmd_vel_smoothed')
        self.declare_parameter('output_topic',      '/cmd_vel')
        self.declare_parameter('depth_topic',
            '/ascamera_hp60c/camera_publisher/depth0/points')
        self.declare_parameter('scan_topic',        '/scan_corrected')
        self.declare_parameter('use_optical_frame', False)

        # Static speed field (walls)
        self.declare_parameter('epsilon',           0.35)
        self.declare_parameter('lam',               2.0)
        self.declare_parameter('min_speed_static',  0.05)  # floor: never full-stop for walls

        # Social / human speed field
        self.declare_parameter('sigma_human',       0.70)
        self.declare_parameter('amplitude_human',   0.95)
        self.declare_parameter('social_max_range',  2.00)

        # Close-range depth camera
        self.declare_parameter('depth_stop_dist',   0.30)
        self.declare_parameter('depth_slow_dist',   0.70)
        self.declare_parameter('depth_height_min',  0.10)
        self.declare_parameter('depth_height_max',  1.80)
        self.declare_parameter('depth_width',       0.30)
        self.declare_parameter('depth_min_points',  5)
        self.declare_parameter('depth_stale_s',     1.0)

        # Leg detector
        self.declare_parameter('leg_cluster_gap',   0.10)
        self.declare_parameter('leg_min_diam',      0.04)
        self.declare_parameter('leg_max_diam',      0.25)
        self.declare_parameter('person_pair_min',   0.10)
        self.declare_parameter('person_pair_max',   0.80)

        p = self.get_parameter
        in_t  = p('input_topic').value
        out_t = p('output_topic').value
        d_t   = p('depth_topic').value
        s_t   = p('scan_topic').value

        self._epsilon        = p('epsilon').value
        self._lam            = p('lam').value
        self._min_static     = p('min_speed_static').value
        self._sigma_h        = p('sigma_human').value
        self._amp_h          = p('amplitude_human').value
        self._soc_range      = p('social_max_range').value
        self._stop_d         = p('depth_stop_dist').value
        self._slow_d         = p('depth_slow_dist').value
        self._h_min          = p('depth_height_min').value
        self._h_max          = p('depth_height_max').value
        self._tunnel_w       = p('depth_width').value
        self._min_pts        = p('depth_min_points').value
        self._depth_stale    = p('depth_stale_s').value
        self._optical        = p('use_optical_frame').value

        # ── state ─────────────────────────────────────────────────────────
        self._depth_min_dist  = math.inf
        self._depth_time      = self.get_clock().now()
        self._humans:         list[tuple[float, float]] = []
        self._speed_model     = None      # loaded lazily when map is ready
        self._robot_xy        = np.zeros(2, dtype=np.float32)

        # ── helpers ───────────────────────────────────────────────────────
        self._leg_det   = LegDetector(
            cluster_gap_m=p('leg_cluster_gap').value,
            leg_min_m=p('leg_min_diam').value,
            leg_max_m=p('leg_max_diam').value,
            pair_min_m=p('person_pair_min').value,
            pair_max_m=p('person_pair_max').value,
        )
        self._tracker   = PersonTracker()
        self._tf_buffer = tf2_ros.Buffer()
        self._tf_listen = tf2_ros.TransformListener(self._tf_buffer, self)

        # ── pubs / subs ───────────────────────────────────────────────────
        self._cmd_pub    = self.create_publisher(Twist, out_t, 10)
        self._status_pub = self.create_publisher(String, '/ntfields/shield_status', 10)
        self._marker_pub = self.create_publisher(MarkerArray, '/ntfields/humans', 10)

        self._cmd_sub   = self.create_subscription(
            Twist, in_t, self._cmd_cb, 10)
        self._depth_sub = self.create_subscription(
            PointCloud2, d_t, self._depth_cb,
            QoSPresetProfiles.SENSOR_DATA.value)
        self._scan_sub  = self.create_subscription(
            LaserScan, s_t, self._scan_cb,
            QoSPresetProfiles.SENSOR_DATA.value)
        self._map_sub   = self.create_subscription(
            __import__('nav_msgs.msg', fromlist=['OccupancyGrid']).OccupancyGrid,
            '/map', self._map_cb, 1)

        self.create_timer(0.2, self._watchdog)

        self.get_logger().info('NTFields Social Shield ready.')

    # ── map → static speed model ──────────────────────────────────────────

    def _map_cb(self, msg):
        from .ntfields import SpeedModel, occupancy_grid_to_distance_field
        dist, _ = occupancy_grid_to_distance_field(
            np.array(msg.data, dtype=np.int8),
            msg.info.width,
            msg.info.height,
            msg.info.resolution,
        )
        sm = SpeedModel(epsilon=self._epsilon, lam=self._lam,
                        sigma=self._sigma_h, amplitude=self._amp_h)
        sm.set_map(dist, msg.info.resolution,
                   msg.info.origin.position.x,
                   msg.info.origin.position.y)
        self._speed_model = sm
        self.get_logger().info('[Shield] Static speed field built from /map.')

    # ── LiDAR scan → human detection ─────────────────────────────────────

    def _scan_cb(self, msg: LaserScan):
        sensor_dets = self._leg_det.detect(msg)
        if not sensor_dets:
            self._humans = self._tracker.update([], self._now())
            return

        # Transform detections from sensor frame to map frame
        world_dets = []
        for sx, sy in sensor_dets:
            try:
                pt = self._tf_buffer.transform_full(
                    self._make_stamped(sx, sy, msg.header),
                    'map',
                    rclpy.time.Time(),
                    'map',
                    rclpy.duration.Duration(seconds=0.1))
                world_dets.append((pt.point.x, pt.point.y))
            except Exception:
                world_dets.append((sx, sy))   # fallback: sensor frame

        self._humans = self._tracker.update(world_dets, self._now())
        if self._speed_model:
            self._speed_model.update_humans(self._humans)

        self._publish_human_markers()

    def _make_stamped(self, x, y, header):
        from geometry_msgs.msg import PointStamped
        ps = PointStamped()
        ps.header = header
        ps.point.x, ps.point.y, ps.point.z = float(x), float(y), 0.0
        return ps

    # ── depth cloud → close-range safety ─────────────────────────────────

    def _depth_cb(self, msg: PointCloud2):
        self._depth_time = self.get_clock().now()
        min_d = math.inf
        count = 0

        for pt in pc2.read_points(msg, field_names=('x', 'y', 'z'),
                                   skip_nans=True):
            rx, ry, rz = pt
            if self._optical:
                fwd, lat, ht = rz, rx, -ry
            else:
                fwd, lat, ht = rx, ry, rz

            if not (self._h_min <= ht <= self._h_max):
                continue
            if not (0.05 <= fwd <= self._slow_d):
                continue
            if abs(lat) > self._tunnel_w:
                continue

            count += 1
            min_d = min(min_d, fwd)

        if count >= self._min_pts:
            self._depth_min_dist = min_d
        else:
            self._depth_min_dist = math.inf

    # ── velocity command ──────────────────────────────────────────────────

    def _cmd_cb(self, msg: Twist):
        S = self._compute_total_speed_scale()
        out = Twist()
        out.angular.z = msg.angular.z                    # always allow rotation

        # Forward motion scaled; reverse always allowed
        if msg.linear.x > 0:
            out.linear.x = msg.linear.x * max(0.0, S)
        else:
            out.linear.x = msg.linear.x                 # reverse: no scaling

        self._cmd_pub.publish(out)

    def _compute_total_speed_scale(self) -> float:
        # 1. Static map component (walls)
        S_static = 1.0
        if self._speed_model and self._speed_model.ready:
            xy  = self._get_robot_xy()
            S_static = float(self._speed_model.static.query_np(
                np.array([xy], dtype=np.float32))[0])
            S_static = max(S_static, self._min_static)

        # 2. Social / human component
        S_social = 1.0
        if self._speed_model and self._humans:
            xy = self._get_robot_xy()
            S_social = float(self._speed_model.social.query_np(
                np.array([xy], dtype=np.float32))[0])

        # 3. Close-range depth component (exponential soft ramp)
        S_depth = 1.0
        d = self._depth_min_dist
        if d < self._slow_d:
            if d <= self._stop_d:
                S_depth = 0.0
            else:
                # Smooth ramp: 0 at stop_d, 1 at slow_d
                t = (d - self._stop_d) / (self._slow_d - self._stop_d + 1e-6)
                S_depth = t * t   # quadratic for smooth onset

        S_total = S_static * S_social * S_depth

        # Publish status
        status = String()
        status.data = (f'S={S_total:.2f} '
                       f'static={S_static:.2f} '
                       f'social={S_social:.2f} '
                       f'depth={S_depth:.2f} '
                       f'humans={len(self._humans)}')
        self._status_pub.publish(status)

        return S_total

    # ── watchdog: clear stale depth data ─────────────────────────────────

    def _watchdog(self):
        dt = (self.get_clock().now() - self._depth_time).nanoseconds / 1e9
        if dt > self._depth_stale:
            self._depth_min_dist = math.inf

    # ── robot pose ────────────────────────────────────────────────────────

    def _get_robot_xy(self) -> np.ndarray:
        try:
            tf = self._tf_buffer.lookup_transform(
                'map', 'base_link', rclpy.time.Time())
            t = tf.transform.translation
            self._robot_xy = np.array([t.x, t.y], dtype=np.float32)
        except Exception:
            pass
        return self._robot_xy

    # ── human markers for RViz ────────────────────────────────────────────

    def _publish_human_markers(self):
        ma = MarkerArray()
        for i, (hx, hy) in enumerate(self._humans):
            m            = Marker()
            m.header.frame_id = 'map'
            m.header.stamp    = self.get_clock().now().to_msg()
            m.ns              = 'humans'
            m.id              = i
            m.type            = Marker.CYLINDER
            m.action          = Marker.ADD
            m.pose.position.x = hx
            m.pose.position.y = hy
            m.pose.position.z = 0.9
            m.pose.orientation.w = 1.0
            m.scale.x = 2 * self._sigma_h
            m.scale.y = 2 * self._sigma_h
            m.scale.z = 0.1
            m.color.r = 1.0
            m.color.g = 0.4
            m.color.a = 0.4
            m.lifetime = rclpy.duration.Duration(seconds=1.0).to_msg()
            ma.markers.append(m)
        self._marker_pub.publish(ma)

    def _now(self) -> float:
        return self.get_clock().now().nanoseconds * 1e-9


def main(args=None):
    rclpy.init(args=args)
    node = NTFieldsSocialShield()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.cmd_pub.publish(Twist()) if hasattr(node, 'cmd_pub') else None
        node.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    main()
