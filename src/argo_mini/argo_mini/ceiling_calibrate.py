#!/usr/bin/env python3
"""Work out the real base_link -> camera transform from the ceiling itself.

The HP60C is mounted facing up and tilted, and the tilt is fixed in hardware.
Both existing descriptions of it are wrong: the URDF still carries the
forward-facing optical joint (rpy -pi/2, 0, -pi/2) and the static publisher in
the launch scripts uses identity. This measures the mount instead of guessing.

Of the mount's three rotational degrees of freedom, the ceiling gives us two for
free. The ceiling is horizontal, so the plane normal *is* base_link's up axis
expressed in camera coordinates, which pins the optical axis relative to
vertical -- both the tilt angle and which way it leans within the image.

The third -- the camera's heading about the vertical axis -- leaves no trace in
a single view of a flat ceiling, because rotating the whole robot about vertical
maps a flat ceiling onto itself. It has to come from motion, which is what
`ceiling_map_builder` measures on a drive. Everything else printed below is
measured here.

There are therefore two ways to supply that third angle:

  ceiling_azimuth_deg  what ceiling_map_builder reports -- the heading of the
                       ceiling-plane basis vector e1 in base_link. Preferred,
                       because it is measured. The builder cannot convert it
                       into a mount rotation itself: that needs the plane
                       normal, which only this node sees.
  mount_azimuth_deg    a hand guess at which way the optical axis leans, for
                       getting a usable TF up before any drive has happened.

Usage:
    ros2 run argo_mini ceiling_calibrate
    ros2 run argo_mini ceiling_calibrate --ros-args -p mount_azimuth_deg:=180.0
    ros2 run argo_mini ceiling_calibrate --ros-args -p ceiling_azimuth_deg:=-87.3
"""

import sys
import time

import numpy as np
import rclpy
from rclpy.node import Node
from rclpy.qos import QoSPresetProfiles
from sensor_msgs.msg import CameraInfo, Image

from argo_mini.ceiling_features import CeilingFeatures

DEPTH_MAX = 4.09


def fit_plane(pts, iters=200, tol=0.05):
    """Delegates to the detector's fit — deliberately one implementation.

    This node used to carry its own copy, and it had stayed on the slow,
    unrefined estimator: 200 hypotheses scored one at a time (~2.8 s a frame,
    against a 90 s budget for 30 frames), with a single polish over a 50 mm
    inlier band. That estimator reads the mount tilt about 2.5 deg high — it
    settled at 25.7 deg where the refined fit gives 23.2 deg — and this node is
    what converts the ceiling normal into the mount TF the whole stack is built
    on. Two nodes fitting the same ceiling by different methods is precisely how
    the mount TF and the landmark projection end up disagreeing.
    """
    return CeilingFeatures.fit_plane(pts, iters=iters, tol=tol)


def rot_between(a, b):
    """Rotation taking unit vector a onto unit vector b."""
    v = np.cross(a, b)
    c = float(a @ b)
    s = float(np.linalg.norm(v))
    if s < 1e-9:
        return np.eye(3) if c > 0 else -np.eye(3)
    vx = np.array([[0, -v[2], v[1]], [v[2], 0, -v[0]], [-v[1], v[0], 0]])
    return np.eye(3) + vx + vx @ vx * ((1 - c) / (s * s))


def mount_rotation(n_opt, az_deg, ceiling_az_deg=float('nan')):
    """Camera -> base_link rotation from the measured normal plus one azimuth.

    Two of the three DOF come from the measurement: sending the ceiling normal
    onto base_link's up axis fixes how far the mount tilts and which way it
    leans. What is left over is a yaw about vertical, and a single view of a
    flat ceiling cannot see it. Whichever azimuth is supplied resolves it; only
    the reference direction being pinned differs, so both go through the same
    `dpsi` here.

    Worth knowing when reading the output: the ceiling azimuth *is* the yaw of
    the mount TF, exactly. e1 differs from the camera's X axis only by a
    multiple of the normal, and R sends the normal to vertical, so the two
    differ only in height once rotated and their headings are identical. So the
    yaw printed below should equal the azimuth ceiling_map_builder reports --
    which makes the whole chain checkable by eye against the TF already running.

    Returns (R, description).
    """
    ez = np.array([0.0, 0.0, 1.0])
    R0 = rot_between(n_opt, ez)                 # sends ceiling normal to body up
    if np.isnan(ceiling_az_deg):
        ref0 = R0 @ ez                          # where the optical axis lands
        target, how = np.radians(az_deg), 'assumed mount azimuth'
    else:
        # The detector's ceiling basis, rebuilt exactly as ceiling_features
        # builds it: optical X projected onto the plane. Matching its heading
        # in base_link is what pins the yaw.
        e1 = np.array([1.0, 0.0, 0.0]) - n_opt[0] * n_opt
        ref0 = R0 @ (e1 / np.linalg.norm(e1))
        target, how = np.radians(ceiling_az_deg), 'measured ceiling azimuth'
    dpsi = target - np.arctan2(ref0[1], ref0[0])
    c, s = np.cos(dpsi), np.sin(dpsi)
    return np.array([[c, -s, 0.0], [s, c, 0.0], [0.0, 0.0, 1.0]]) @ R0, how


def rpy_from_matrix(R):
    """ROS fixed-axis XYZ, matching static_transform_publisher's --roll/--pitch/--yaw."""
    sy = -R[2, 0]
    sy = max(-1.0, min(1.0, sy))
    pitch = np.arcsin(sy)
    if abs(sy) < 0.999999:
        roll = np.arctan2(R[2, 1], R[2, 2])
        yaw = np.arctan2(R[1, 0], R[0, 0])
    else:                                    # gimbal lock
        roll = np.arctan2(-R[1, 2], R[1, 1])
        yaw = 0.0
    return roll, pitch, yaw


class Calib(Node):
    def __init__(self):
        super().__init__('ceiling_calibrate')
        self.declare_parameter('mount_azimuth_deg', 0.0)
        self.declare_parameter('ceiling_azimuth_deg', float('nan'))
        self.declare_parameter('frames', 30)
        self.declare_parameter('depth_topic',
                               '/ascamera_hp60c/camera_publisher/depth0/image_raw')
        self.declare_parameter('info_topic',
                               '/ascamera_hp60c/camera_publisher/rgb0/camera_info')
        # From the URDF camera_joint — the translation is not in question here.
        self.declare_parameter('xyz', [0.2575, 0.0, 0.170])

        self.K = None
        self.fits = []
        q = QoSPresetProfiles.SENSOR_DATA.value
        self.create_subscription(CameraInfo,
                                 self.get_parameter('info_topic').value,
                                 self.on_info, q)
        self.create_subscription(Image,
                                 self.get_parameter('depth_topic').value,
                                 self.on_depth, q)

    def on_info(self, m):
        if self.K is None:
            self.K = (m.k[0], m.k[4], m.k[2], m.k[5])

    def on_depth(self, m):
        want = int(self.get_parameter('frames').value)
        if self.K is None or len(self.fits) >= want:
            return
        d = np.frombuffer(m.data, np.uint16).reshape(
            m.height, m.width).astype(np.float32) / 1000.0
        fx, fy, cx, cy = self.K
        step = 4
        ds = d[::step, ::step]
        u, v = np.meshgrid(np.arange(0, m.width, step),
                           np.arange(0, m.height, step))
        ok = (ds > 0.05) & (ds < DEPTH_MAX)
        if ok.sum() < 500:
            return
        pts = np.stack([(u[ok] - cx) / fx * ds[ok],
                        (v[ok] - cy) / fy * ds[ok], ds[ok]], axis=1)
        n, off = fit_plane(pts)
        if n is not None:
            self.fits.append((n, off))
            print(f'  frame {len(self.fits)}/{want}', end='\r', flush=True)


def main(args=None):
    rclpy.init(args=args)
    node = Calib()
    want = int(node.get_parameter('frames').value)
    t0 = time.time()
    while rclpy.ok() and len(node.fits) < want and time.time() - t0 < 90:
        rclpy.spin_once(node, timeout_sec=2.0)
    az = float(node.get_parameter('mount_azimuth_deg').value)
    ceil_az = float(node.get_parameter('ceiling_azimuth_deg').value)
    xyz = list(node.get_parameter('xyz').value)
    node.destroy_node()
    rclpy.shutdown()

    if len(node.fits) < 5:
        print('\nnot enough depth frames — is the camera node running?')
        return 1

    N = np.array([f[0] for f in node.fits])
    offs = np.array([f[1] for f in node.fits])
    n_opt = np.median(N, axis=0)
    n_opt /= np.linalg.norm(n_opt)
    tilt = np.degrees(np.arccos(min(1.0, abs(n_opt[2]))))
    spread = np.degrees(np.arccos(np.clip(N @ n_opt, -1, 1))).max()

    print(f'\n\nceiling normal in camera coords: '
          f'[{n_opt[0]:+.4f} {n_opt[1]:+.4f} {n_opt[2]:+.4f}]')
    print(f'  tilt off vertical : {tilt:.2f} deg   (frame spread {spread:.2f} deg)')
    print(f'  ceiling height    : {np.median(offs):.3f} m above the camera '
          f'(std {offs.std()*1000:.1f} mm)')

    R, how = mount_rotation(n_opt, az, ceil_az)
    ez = np.array([0.0, 0.0, 1.0])
    zb = R @ ez
    roll, pitch, yaw = rpy_from_matrix(R)
    resid = np.degrees(np.arccos(min(1.0, float((R @ n_opt) @ ez))))

    if np.isnan(ceil_az):
        print(f'\n{how}: {az:.1f} deg '
              f'(0 = optical axis leans toward robot front, 90 = toward robot left)')
    else:
        print(f'\n{how}: {ceil_az:.2f} deg  (from ceiling_map_builder; '
              f'0 = ceiling basis e1 points to robot front, 90 = to robot left)')
        zb_h = np.hypot(zb[0], zb[1])
        if zb_h > 1e-6:
            print(f'  implies mount_azimuth_deg '
                  f'{np.degrees(np.arctan2(zb[1], zb[0])):+.2f} — the direction '
                  f'the optical axis leans')
    print(f'  optical axis in base_link: '
          f'[{zb[0]:+.3f} {zb[1]:+.3f} {zb[2]:+.3f}]')
    print(f'  check — ceiling normal maps to base_link up within {resid:.3f} deg')

    print('\nstatic_transform_publisher arguments:\n')
    print(f'  --x {xyz[0]} --y {xyz[1]} --z {xyz[2]} \\')
    print(f'  --roll {roll:.6f} --pitch {pitch:.6f} --yaw {yaw:.6f} \\')
    print('  --frame-id base_link --child-frame-id ascamera_hp60c_color_0')
    print(f'\n  (roll {np.degrees(roll):+.2f} deg, pitch {np.degrees(pitch):+.2f} deg, '
          f'yaw {np.degrees(yaw):+.2f} deg)')
    if np.isnan(ceil_az):
        print('\nThe azimuth is the one value not measured here — run '
              '`ros2 run argo_mini ceiling_map_builder`, drive the robot, and '
              'feed the number it prints back as ceiling_azimuth_deg.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
