"""
Speed field computation for NTFields 2D.

Two sources:
  1. Online (Mode 1.5): approximate S*(q) from a live 2D lidar scan
  2. Offline (Mode 2B): exact S*(q) from a SLAM occupancy grid via EDT
"""

import math
import numpy as np
import torch
from scipy.ndimage import distance_transform_edt


# ── Online: lidar scan → speed samples ──────────────────────────────────────

def scan_to_speed_samples(
    ranges:          np.ndarray,
    angle_min:       float,
    angle_increment: float,
    robot_x: float,
    robot_y: float,
    robot_theta: float,
    n_rays:    int   = 180,
    n_strat:   int   = 10,
    d_min_m:   float = 0.07,
    d_max_m:   float = 0.70,
    max_range: float = 10.0,
) -> tuple:
    """
    Convert a 2D lidar scan to (points, speeds) training pairs.

    Implements paper §IV-E-1 for 2D:
      - Sample n_rays rays; stratify n_strat points along each
      - Speed at each point = clip(dist_to_nearest_surface, d_min, d_max) / d_max
      - Pairs formed by random cross-matching within the frame

    Args:
        ranges:          lidar range array (all rays, metres)
        angle_min:       first ray angle (rad, robot frame)
        angle_increment: rad per ray
        robot_{x,y,theta}: robot pose in world (map) frame
        n_rays:          rays to sample
        n_strat:         stratified samples per ray
        d_min_m, d_max_m: speed bounds (metres)
        max_range:       ignore rays longer than this

    Returns:
        (points, speeds) tensors of shape (N, 4) and (N, 2) in world frame,
        or (None, None) if the scan has too few valid rays.
    """
    n_total = len(ranges)
    angles  = angle_min + np.arange(n_total, dtype=np.float32) * angle_increment

    valid = np.isfinite(ranges) & (ranges > 0.1) & (ranges < max_range)
    vidx  = np.where(valid)[0]
    if len(vidx) < 4:
        return None, None

    chosen = vidx[np.random.choice(len(vidx), min(n_rays, len(vidx)), replace=False)]

    c, s = math.cos(robot_theta), math.sin(robot_theta)
    R    = np.array([[c, -s], [s, c]], dtype=np.float32)
    orig = np.array([robot_x, robot_y], dtype=np.float32)

    # Surface endpoints in world frame (for distance approximation)
    surf_local = np.column_stack([
        ranges[vidx] * np.cos(angles[vidx]),
        ranges[vidx] * np.sin(angles[vidx]),
    ]).astype(np.float32)
    surf_world = (R @ surf_local.T).T + orig

    all_pts, all_dists = [], []
    for idx in chosen:
        ray_r = float(ranges[idx])
        ang   = float(angles[idx])
        t_pts = (np.linspace(0.0, ray_r, n_strat + 1)[:-1]
                 + np.linspace(0.0, ray_r, n_strat + 1)[1:]) / 2.0

        pts_local = np.column_stack([
            t_pts * math.cos(ang),
            t_pts * math.sin(ang),
        ]).astype(np.float32)
        pts_world = (R @ pts_local.T).T + orig

        # Approximate distance to nearest surface point
        diff  = pts_world[:, None, :] - surf_world[None, :, :]
        dists = np.linalg.norm(diff, axis=-1).min(axis=1)

        all_pts.append(pts_world)
        all_dists.append(dists)

    pts   = np.vstack(all_pts)
    dists = np.concatenate(all_dists)
    spd   = np.clip(dists, d_min_m, d_max_m) / d_max_m

    N  = pts.shape[0]
    si = np.random.permutation(N)
    gi = np.random.permutation(N)
    paired_pts = np.concatenate([pts[si], pts[gi]], axis=1).astype(np.float32)
    paired_spd = np.column_stack([spd[si], spd[gi]]).astype(np.float32)

    return (
        torch.from_numpy(paired_pts),
        torch.from_numpy(paired_spd),
    )


# ── Offline: occupancy grid → exact EDT speed function ───────────────────────

def occupancy_to_edt(occ_data: np.ndarray, resolution: float,
                     d_min_m: float = 0.07, d_max_m: float = 0.70):
    """
    Build a speed callable from a SLAM occupancy grid using exact EDT.

    Args:
        occ_data:   (H, W) ROS occupancy  (0=free, 100=occupied, -1=unknown)
        resolution: metres per cell
        d_min_m, d_max_m: speed bounds (metres)

    Returns:
        speed_fn: (col_arr, row_arr) → speed_arr in [0, 1]
        dist_m:   (H, W) distance image in metres (for debugging)
    """
    # EDT: True=free(object) → distance to False(obstacle=background)
    free_mask  = occ_data < 50
    dist_cells = distance_transform_edt(free_mask)
    dist_m     = dist_cells * resolution

    H, W = occ_data.shape

    def speed_fn(cols: np.ndarray, rows: np.ndarray) -> np.ndarray:
        c = np.clip(np.round(cols).astype(int), 0, W - 1)
        r = np.clip(np.round(rows).astype(int), 0, H - 1)
        return np.clip(dist_m[r, c], d_min_m, d_max_m) / d_max_m

    return speed_fn, dist_m


def sample_training_pairs_from_map(
    occ_data:   np.ndarray,
    resolution: float,
    origin_xy:  np.ndarray,       # world coords of (col=0, row=H-1) in ROS convention
    normalizer,                    # CoordNormalizer instance
    n_pairs:    int   = 200_000,
    d_min_m:    float = 0.07,
    d_max_m:    float = 0.70,
) -> tuple:
    """
    Build the full Mode 2B offline training dataset from a SLAM map.

    ROS map convention:
        world_x = origin_x + col * resolution
        world_y = origin_y + (H - 1 - row) * resolution   (row 0 = top = max y)

    Returns:
        points_norm : (N, 4)  normalised model-space [qs | qg]
        speeds      : (N, 2)  [S*(qs), S*(qg)] in (0, 1]
    """
    speed_fn, _ = occupancy_to_edt(occ_data, resolution, d_min_m, d_max_m)

    H, W      = occ_data.shape
    free_mask = occ_data < 50
    frows, fcols = np.where(free_mask)
    if len(frows) == 0:
        raise ValueError("No free cells in occupancy grid")

    n_draw = n_pairs * 2
    idx    = np.random.choice(len(frows), n_draw, replace=True)
    cols   = fcols[idx]
    rows   = frows[idx]

    # World coords (ROS convention: row 0 = top = highest y)
    x_w = origin_xy[0] + cols * resolution
    y_w = origin_xy[1] + (H - 1 - rows) * resolution
    xy_w = np.column_stack([x_w, y_w]).astype(np.float32)

    # Normalise
    xy_n = normalizer.to_model(xy_w)

    si = np.random.permutation(n_draw)[:n_pairs]
    gi = np.random.permutation(n_draw)[:n_pairs]
    points_norm = np.concatenate([xy_n[si], xy_n[gi]], axis=1).astype(np.float32)

    spd_s = speed_fn(cols[si].astype(float), rows[si].astype(float))
    spd_g = speed_fn(cols[gi].astype(float), rows[gi].astype(float))
    speeds = np.column_stack([spd_s, spd_g]).astype(np.float32)

    return (
        torch.from_numpy(points_norm),
        torch.from_numpy(speeds),
    )
