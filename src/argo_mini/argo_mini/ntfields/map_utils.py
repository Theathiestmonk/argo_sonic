"""
Map utilities: OccupancyGrid → distance field + coordinate helpers.

ROS2 OccupancyGrid convention
  data  : row-major int8 array  (0=free, 100=occupied, -1=unknown)
  origin: bottom-left corner in world frame
  width / height in cells
  resolution in m/cell
"""

import numpy as np
from scipy.ndimage import distance_transform_edt


def occupancy_grid_to_distance_field(
    data: np.ndarray,
    width: int,
    height: int,
    resolution: float,
    unknown_as_obstacle: bool = True,
    inflation_m: float = 0.0,
) -> tuple[np.ndarray, np.ndarray]:
    """
    Convert a flat OccupancyGrid data array to a metric distance field.

    Returns
    -------
    dist   : (height, width) float32  — distance to nearest obstacle in metres
    free   : (height, width) bool     — True where robot can safely be
    """
    grid = np.array(data, dtype=np.int8).reshape((height, width))

    obstacle = grid >= 50
    if unknown_as_obstacle:
        obstacle |= grid < 0

    # Euclidean distance transform (in pixels)
    dist_px = distance_transform_edt(~obstacle).astype(np.float32)
    dist_m  = dist_px * resolution

    if inflation_m > 0:
        dist_m = np.maximum(dist_m - inflation_m, 0.0)

    free = dist_m > 0.0
    return dist_m, free


def world_to_grid(
    x: float, y: float,
    origin_x: float, origin_y: float,
    resolution: float,
    height: int, width: int,
) -> tuple[int, int] | None:
    """
    World coordinates (m) → grid (row, col).  Returns None if out of bounds.
    """
    col = int((x - origin_x) / resolution)
    row = int((y - origin_y) / resolution)
    if 0 <= row < height and 0 <= col < width:
        return row, col
    return None


def grid_to_world(
    row: int, col: int,
    origin_x: float, origin_y: float,
    resolution: float,
) -> tuple[float, float]:
    """Grid (row, col) → world-frame centre of that cell (m)."""
    x = col * resolution + origin_x + resolution * 0.5
    y = row * resolution + origin_y + resolution * 0.5
    return x, y


def sample_free_positions(
    dist_field: np.ndarray,
    resolution: float,
    origin_x: float,
    origin_y: float,
    n: int,
    min_clearance_m: float = 0.15,
) -> np.ndarray:
    """
    Sample n random (x, y) positions in free space with at least
    min_clearance_m from any obstacle.

    Returns (n, 2) float32 array in world coordinates.
    """
    mask = dist_field >= min_clearance_m
    rows, cols = np.where(mask)

    if len(rows) == 0:
        raise RuntimeError('No free cells found — check map and clearance.')

    idx = np.random.choice(len(rows), size=n, replace=(len(rows) < n))
    r, c = rows[idx], cols[idx]

    # Add sub-cell noise so the network sees the full continuous space
    noise = np.random.uniform(-0.4, 0.4, (n, 2)) * resolution
    x = c * resolution + origin_x + resolution * 0.5 + noise[:, 0]
    y = r * resolution + origin_y + resolution * 0.5 + noise[:, 1]

    return np.stack([x, y], axis=1).astype(np.float32)


def path_to_world_coords(
    path_cells: list[tuple[int, int]],
    origin_x: float,
    origin_y: float,
    resolution: float,
) -> np.ndarray:
    """List of (row, col) grid cells → (N, 2) world-frame array."""
    pts = [grid_to_world(r, c, origin_x, origin_y, resolution)
           for r, c in path_cells]
    return np.array(pts, dtype=np.float32)
