#!/usr/bin/env python3
"""Convert a ROS occupancy map (map.yaml + map.pgm) into a 3D wall model.

    python map_to_3d.py --map map.yaml --height 2.5 --output argo_3d_map.glb

Writes <stem>.glb, <stem>.obj, <stem>.stl, <stem>_preview.png (3D render),
<stem>_topdown.png (walls + doorways over the source map) and <stem>_meta.json.

Units are metres. OBJ/STL keep the ROS frame (Z up, same origin/yaw as the map).
GLB follows the glTF convention (Y up): (x, y, z)_ros -> (x, z, -y), which is what
three.js expects and what Blender's glTF importer converts back to Z-up.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass
from pathlib import Path

try:
    import cv2
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np
    import shapely
    import trimesh
    import yaml
    from matplotlib.collections import LineCollection, PolyCollection
    from matplotlib.patches import PathPatch, Patch
    from matplotlib.path import Path as MplPath
    from matplotlib.transforms import Affine2D
    from shapely import affinity
    from shapely.geometry import Polygon
    from shapely.geometry.polygon import orient
except ImportError as exc:
    sys.exit(f"Missing dependency '{exc.name}'. Install with: pip install -r requirements-map3d.txt")

if not hasattr(shapely, "constrained_delaunay_triangles"):
    sys.exit("shapely >= 2.1 is required (pip install -U 'shapely>=2.1').")


class MapError(Exception):
    """Bad or unreadable input map."""


def log(msg: str) -> None:
    print(f"[map_to_3d] {msg}", flush=True)


# ── Loading and classification ───────────────────────────────────────────────


@dataclass
class RosMap:
    resolution: float
    origin: tuple[float, float, float]
    occupied: np.ndarray
    free: np.ndarray
    unknown: np.ndarray
    image_path: Path
    yaml_path: Path

    @property
    def height_px(self) -> int:
        return self.occupied.shape[0]

    @property
    def width_px(self) -> int:
        return self.occupied.shape[1]


def load_map(yaml_path: Path, unknown_value: int) -> RosMap:
    try:
        meta = yaml.safe_load(yaml_path.read_text())
    except (OSError, yaml.YAMLError) as exc:
        raise MapError(f"cannot read {yaml_path}: {exc}") from exc
    if not isinstance(meta, dict):
        raise MapError(f"{yaml_path} is not a valid map YAML")
    for key in ("image", "resolution", "origin"):
        if key not in meta:
            raise MapError(f"{yaml_path} is missing required key '{key}'")

    try:
        resolution = float(meta["resolution"])
        ox, oy, yaw = (float(v) for v in list(meta["origin"])[:3])
    except (TypeError, ValueError) as exc:
        raise MapError(f"bad resolution/origin in {yaml_path}: {exc}") from exc
    if resolution <= 0:
        raise MapError(f"resolution must be > 0 (got {resolution})")

    negate = int(meta.get("negate", 0))
    occ_th = float(meta.get("occupied_thresh", 0.65))
    free_th = float(meta.get("free_thresh", 0.196))
    if not 0.0 <= free_th < occ_th <= 1.0:
        raise MapError(f"need 0 <= free_thresh < occupied_thresh <= 1 (got {free_th}, {occ_th})")
    mode = str(meta.get("mode", "trinary")).lower()
    if mode != "trinary":
        log(f"warning: map mode '{mode}' is treated as 'trinary' (thresholds applied)")

    image_path = Path(str(meta["image"])).expanduser()
    if not image_path.is_absolute():
        image_path = yaml_path.parent / image_path
    if not image_path.is_file():
        raise MapError(f"map image not found: {image_path}")
    gray = cv2.imread(str(image_path), cv2.IMREAD_UNCHANGED)
    if gray is None:
        raise MapError(f"OpenCV could not decode {image_path}")
    if gray.ndim == 3:
        gray = cv2.cvtColor(gray[:, :, :3], cv2.COLOR_BGR2GRAY)
    if gray.dtype not in (np.uint8, np.uint16):
        raise MapError(f"unsupported image depth {gray.dtype} in {image_path}")

    maxval = float(np.iinfo(gray.dtype).max)
    p = gray.astype(np.float32) / maxval
    occ_prob = p if negate else 1.0 - p
    occupied = occ_prob > occ_th
    free = occ_prob < free_th

    # map_saver writes unknown as 205. With free_thresh 0.25 the strict ROS formula
    # scores that as (255-205)/255 = 0.196 < 0.25, i.e. *free*. Treat it as unknown.
    if unknown_value >= 0 and gray.dtype == np.uint8:
        is_unknown = gray == unknown_value
        occupied &= ~is_unknown
        free &= ~is_unknown
    unknown = ~(occupied | free)

    log(
        f"map {gray.shape[1]}x{gray.shape[0]} px @ {resolution} m/px = "
        f"{gray.shape[1] * resolution:.2f} x {gray.shape[0] * resolution:.2f} m, "
        f"origin=({ox}, {oy}, yaw {yaw}); occupied={int(occupied.sum())} "
        f"free={int(free.sum())} unknown={int(unknown.sum())} px"
    )
    return RosMap(resolution, (ox, oy, yaw), occupied, free, unknown, image_path, yaml_path)


# ── Wall mask cleanup ────────────────────────────────────────────────────────


def line_kernel(length: int, angle_deg: float) -> np.ndarray:
    """Thin rasterised line, used to bridge gaps along a chosen direction."""
    k = np.zeros((length, length), np.uint8)
    c = (length - 1) / 2.0
    dx, dy = math.cos(math.radians(angle_deg)) * c, math.sin(math.radians(angle_deg)) * c
    cv2.line(k, (round(c - dx), round(c - dy)), (round(c + dx), round(c + dy)), 1, 1)
    return k


def bridge_small_gaps(mask: np.ndarray, res: float, gap_m: float) -> np.ndarray:
    """Close lidar drop-outs (dashed walls). Only runs <= gap_m are bridged, so real
    doorways stay open. Axis-aligned only: diagonal kernels chamfer every inside corner."""
    length = int(round(gap_m / res)) + 1
    if length < 3:
        return mask
    src = mask.astype(np.uint8)
    out = mask.copy()
    for angle in (0, 90):
        out |= cv2.morphologyEx(src, cv2.MORPH_CLOSE, line_kernel(length, angle)) > 0
    return out


def remove_small_components(mask: np.ndarray, res: float, min_feature_m: float) -> np.ndarray:
    """Drop speckle: 8-connected blobs whose longest bounding-box side is below the limit."""
    min_px = max(1, math.ceil(min_feature_m / res))
    n, labels, stats, _ = cv2.connectedComponentsWithStats(mask.astype(np.uint8), connectivity=8)
    keep = np.maximum(stats[:, cv2.CC_STAT_WIDTH], stats[:, cv2.CC_STAT_HEIGHT]) >= min_px
    keep[0] = False
    return keep[labels]


def make_4connected(mask: np.ndarray) -> np.ndarray:
    """Add one pixel at each diagonal-only contact so 1px diagonal walls become one
    polygon instead of a chain of squares touching at corners."""
    out = mask.copy()
    a, b, c, d = mask[:-1, :-1], mask[:-1, 1:], mask[1:, :-1], mask[1:, 1:]
    out[:-1, 1:] |= a & d & ~b & ~c
    out[:-1, :-1] |= b & c & ~a & ~d
    return out


# ── Mask -> polygons (map-local frame, metres, origin at lower-left corner) ──


def mask_to_polygons(mask: np.ndarray, res: float, simplify_m: float) -> list[Polygon]:
    """Exact union of occupied cells (wall thickness is preserved to the pixel), then a
    Douglas-Peucker pass so straight walls collapse to a few vertices."""
    h, w = mask.shape
    padded = np.zeros((h, w + 2), np.int8)
    padded[:, 1:-1] = make_4connected(mask)
    diff = np.diff(padded, axis=1)
    rows, starts = np.nonzero(diff == 1)
    _, ends = np.nonzero(diff == -1)
    if rows.size == 0:
        return []
    boxes = shapely.box(starts * res, (h - rows - 1) * res, ends * res, (h - rows) * res)
    merged = shapely.union_all(boxes)
    parts = list(merged.geoms) if hasattr(merged, "geoms") else [merged]

    # Simplifying a 1px wall can pinch it to zero width, leaving rings that touch at a point:
    # a valid polygon but a non-manifold solid. Shrinking by ~2% of a cell (1 mm at 5 cm/px)
    # merges such rings. The exactly-collinear vertices are then dropped because earcut
    # removes them from the caps, which would otherwise leave T-junctions against the sides.
    eps = 0.02 * res
    polys: list[Polygon] = []
    for g in parts:
        g = g.simplify(simplify_m, preserve_topology=True)
        if not g.is_valid:
            g = g.buffer(0)
        g = g.buffer(-eps, join_style="mitre").simplify(1e-7, preserve_topology=True)
        for sub in getattr(g, "geoms", [g]):
            if isinstance(sub, Polygon) and not sub.is_empty and sub.area > (0.5 * res) ** 2:
                polys.append(sub)
    return polys


def to_map_frame(geom, origin: tuple[float, float, float]):
    ox, oy, yaw = origin
    c, s = math.cos(yaw), math.sin(yaw)
    return affinity.affine_transform(geom, [c, -s, s, c, ox, oy])


# ── Doorway detection ────────────────────────────────────────────────────────


@dataclass
class Doorway:
    polygon: Polygon  # map-local frame
    center: tuple[float, float]  # map-local frame
    width: float
    depth: float
    angle: float  # direction of the opening (along the wall), map-local frame


def detect_doorways(
    occ: np.ndarray, free: np.ndarray, res: float, door_min: float, door_max: float, max_depth: float = 0.45
) -> list[Doorway]:
    """A doorway is a gap between two wall ends: free cells that a straight line of length
    <= door_max bridges between occupied cells, forming a short, thin, rectangular slot
    with free space on both sides. Walls themselves are never modified by this step."""
    h, w = occ.shape
    length = int(math.ceil(door_max / res)) + 1
    length += (length % 2 == 0)
    occ_u8 = occ.astype(np.uint8)
    found: list[tuple[float, Doorway]] = []
    reach = math.ceil(0.6 / res)  # wall must continue this far past each end of the slot

    def sample(mask: np.ndarray, x: float, y: float) -> bool:
        xi, yi = math.floor(x + 0.5), math.floor(y + 0.5)
        return 0 <= xi < w and 0 <= yi < h and bool(mask[yi, xi])

    def wall_runs_along(cx, cy, sgn, along, normal, short_px, long_px) -> bool:
        """Is the strip beyond this end of the slot solid wall, i.e. does a wall continue in line
        with the opening? (A perpendicular stub next to the door, e.g. a T-junction, doesn't matter.)
        The strip is sampled strictly inside the slot depth, which equals the wall thickness."""
        half = max(0.0, short_px / 2 - 0.5)
        dist = np.arange(1.5, reach + 1.0, 1.0)[:, None]
        lat = np.linspace(-half, half, max(1, round(short_px)))[None, :]
        xs = cx + sgn * along[0] * (long_px / 2 + dist) + normal[0] * lat
        ys = cy + sgn * along[1] * (long_px / 2 + dist) + normal[1] * lat
        hits = [sample(occ, x, y) for x, y in zip(xs.ravel(), ys.ravel())]
        return float(np.mean(hits)) >= 0.7

    for angle in range(0, 180, 15):
        fill = (cv2.morphologyEx(occ_u8, cv2.MORPH_CLOSE, line_kernel(length, angle)) > 0) & ~occ
        if not fill.any():
            continue
        n, labels, stats, _ = cv2.connectedComponentsWithStats(fill.astype(np.uint8), connectivity=8)
        for i in range(1, n):
            x0, y0, bw, bh, area = stats[i]
            if not (door_min * 0.8 <= math.hypot(bw, bh) * res <= door_max * 1.6):
                continue
            ys, xs = np.nonzero(labels[y0 : y0 + bh, x0 : x0 + bw] == i)
            pts = np.column_stack([xs + x0, ys + y0]).astype(np.float32)
            (cx, cy), (rw, rh), ang = cv2.minAreaRect(pts)
            rw, rh = rw + 1.0, rh + 1.0  # pixel centres -> full pixel extents
            long_px, short_px = max(rw, rh), min(rw, rh)
            width, depth = long_px * res, short_px * res
            if not (door_min <= width <= door_max and depth <= max_depth):
                continue
            if area / (long_px * short_px) < 0.6:
                continue

            box = cv2.boxPoints(((cx, cy), (rw, rh), ang))
            e1, e2 = box[1] - box[0], box[2] - box[1]
            along = e1 if np.linalg.norm(e1) >= np.linalg.norm(e2) else e2
            along = along / np.linalg.norm(along)
            normal = np.array([-along[1], along[0]])

            # Both ends must be an elongated wall running along the slot (collinear), which
            # rejects thin slivers bridged between unrelated clutter.
            if not all(wall_runs_along(cx, cy, sgn, along, normal, short_px, long_px) for sgn in (-1, 1)):
                continue
            # Free space must run along the whole slot on both sides (a real passage).
            span = np.linspace(-0.4, 0.4, 7) * long_px
            sides_free = True
            for sgn in (-1, 1):
                off = sgn * normal * (short_px / 2 + 2.5)
                px_ = cx + off[0] + span * along[0]
                py_ = cy + off[1] + span * along[1]
                clear = np.array([not sample(occ, x, y) for x, y in zip(px_, py_)])
                is_free = np.array([sample(free, x, y) for x, y in zip(px_, py_)])
                if clear.mean() < 0.85 or is_free.mean() < 0.5:
                    sides_free = False
            if not sides_free:
                continue

            def local(px: float, py: float) -> tuple[float, float]:
                return ((px + 0.5) * res, (h - (py + 0.5)) * res)

            poly = Polygon([local(x, y) for x, y in box])
            ax_, ay_ = local(cx, cy)
            direction = (math.atan2(-along[1], along[0]) + math.pi / 2) % math.pi - math.pi / 2  # line, not arrow
            found.append((area, Doorway(poly, (ax_, ay_), width, depth, direction)))

    found.sort(key=lambda t: -t[0])
    doors: list[Doorway] = []
    for _, cand in found:
        if all(math.dist(cand.center, d.center) > 0.5 * max(cand.width, d.width) for d in doors):
            doors.append(cand)
    doors.sort(key=lambda d: (round(d.center[1], 1), d.center[0]))
    return doors


# ── Meshing ──────────────────────────────────────────────────────────────────


def triangulate(poly: Polygon) -> tuple[np.ndarray, np.ndarray]:
    """Constrained Delaunay triangulation: every ring vertex is a node, so the caps match the
    side walls exactly. (earcut skips vertices lying exactly on another triangle's edge,
    which leaves T-junctions on polygons whose hole edge is collinear with the shell.)"""
    tri = shapely.constrained_delaunay_triangles(poly)
    corners = np.array([g.exterior.coords[:3] for g in tri.geoms], float).reshape(-1, 2)
    verts, inverse = np.unique(corners, axis=0, return_inverse=True)
    return verts, inverse.reshape(-1, 3)


def prism(poly: Polygon, z0: float, z1: float) -> trimesh.Trimesh:
    """Watertight prism: triangulated caps + one quad per ring edge. Built by hand because
    trimesh.extrude_polygon can emit non-manifold edges for polygons with holes."""
    poly = orient(poly, 1.0)  # shell CCW, holes CW -> side normals point away from material
    v2, tris = triangulate(poly)
    a, b, c = v2[tris[:, 0]], v2[tris[:, 1]], v2[tris[:, 2]]
    cross = (b[:, 0] - a[:, 0]) * (c[:, 1] - a[:, 1]) - (b[:, 1] - a[:, 1]) * (c[:, 0] - a[:, 0])
    tris = np.where((cross < 0)[:, None], tris[:, ::-1], tris)
    n = len(v2)
    verts = [np.column_stack([v2, np.full(n, z0)]), np.column_stack([v2, np.full(n, z1)])]
    faces = [tris[:, ::-1], tris + n]  # bottom faces down, top faces up
    offset = 2 * n
    for ring in (poly.exterior, *poly.interiors):
        xy = np.asarray(ring.coords)[:-1]
        m = len(xy)
        verts += [np.column_stack([xy, np.full(m, z0)]), np.column_stack([xy, np.full(m, z1)])]
        i = np.arange(m)
        j = (i + 1) % m
        faces += [np.column_stack([offset + i, offset + j, offset + m + j]),
                  np.column_stack([offset + i, offset + m + j, offset + m + i])]
        offset += 2 * m
    mesh = trimesh.Trimesh(np.concatenate(verts), np.concatenate(faces), process=False)
    mesh.merge_vertices()
    return mesh


def extrude(polys: list[Polygon], z0: float, z1: float) -> trimesh.Trimesh | None:
    """One watertight body per polygon, concatenated (bodies are deliberately not welded)."""
    parts, skipped = [], 0
    for p in polys:
        try:
            parts.append(prism(p, z0, z1))
        except Exception:  # degenerate polygon the triangulator rejects
            skipped += 1
    if skipped:
        log(f"warning: skipped {skipped} polygon(s) the triangulator rejected")
    return trimesh.util.concatenate(parts) if parts else None


def pbr(mesh: trimesh.Trimesh, rgb: tuple[int, int, int]) -> trimesh.Trimesh:
    out = mesh.copy()
    out.visual = trimesh.visual.TextureVisuals(
        material=trimesh.visual.material.PBRMaterial(
            baseColorFactor=[*rgb, 255], metallicFactor=0.0, roughnessFactor=0.9, doubleSided=False
        )
    )
    return out


# glTF is Y-up: rotate -90 deg about X so ROS (x, y, z) -> (x, z, -y).
ROS_TO_GLTF = np.array([[1, 0, 0, 0], [0, 0, 1, 0], [0, -1, 0, 0], [0, 0, 0, 1]], float)


# ── Previews ─────────────────────────────────────────────────────────────────


def polygon_path(poly: Polygon) -> MplPath:
    poly = orient(poly, 1.0)
    verts, codes = [], []
    for ring in [poly.exterior, *poly.interiors]:
        xy = np.asarray(ring.coords)
        verts.extend(xy)
        codes.extend([MplPath.MOVETO] + [MplPath.LINETO] * (len(xy) - 2) + [MplPath.CLOSEPOLY])
    return MplPath(verts, codes)


def render_topdown(path: Path, m: RosMap, walls: list[Polygon], doors: list[Doorway], title: str) -> None:
    res, (ox, oy, yaw) = m.resolution, m.origin
    h, w = m.height_px, m.width_px
    rgb = np.empty((h, w, 3), np.float32)
    rgb[:] = (0.80, 0.80, 0.80)
    rgb[m.free] = (1.0, 1.0, 1.0)
    rgb[m.occupied] = (0.10, 0.10, 0.12)

    fig, ax = plt.subplots(figsize=(13, 13 * max(0.35, min(1.0, h / w))), dpi=140)
    to_world = Affine2D().rotate(yaw).translate(ox, oy)
    rgba = np.dstack([rgb, np.ones((h, w), np.float32)])  # alpha keeps the area outside a rotated map transparent
    ax.imshow(rgba, extent=(0, w * res, 0, h * res), origin="upper", interpolation="nearest",
              transform=to_world + ax.transData, zorder=0)
    corners = to_world.transform([(0, 0), (w * res, 0), (w * res, h * res), (0, h * res)])
    pad = 0.5
    ax.set_xlim(corners[:, 0].min() - pad, corners[:, 0].max() + pad)
    ax.set_ylim(corners[:, 1].min() - pad, corners[:, 1].max() + pad)
    ax.set_aspect("equal")

    for p in walls:
        ax.add_patch(PathPatch(polygon_path(p), facecolor=(0.85, 0.15, 0.15, 0.30),
                               edgecolor=(0.85, 0.1, 0.1), linewidth=0.9, zorder=2))
    for i, d in enumerate(doors):
        ax.add_patch(PathPatch(polygon_path(d.polygon), facecolor=(1.0, 0.6, 0.0, 0.55),
                               edgecolor=(0.9, 0.45, 0.0), linewidth=1.2, zorder=3))
        cx, cy = d.center
        ax.annotate(f"{d.width:.2f} m", (cx, cy), xytext=(0, 9), textcoords="offset points",
                    ha="center", fontsize=7, color=(0.7, 0.35, 0.0), zorder=4)
    ax.annotate("", xy=(ox + 0.6 * math.cos(yaw), oy + 0.6 * math.sin(yaw)), xytext=(ox, oy),
                arrowprops=dict(arrowstyle="->", color="crimson", lw=2), zorder=5)
    ax.annotate("", xy=(ox - 0.6 * math.sin(yaw), oy + 0.6 * math.cos(yaw)), xytext=(ox, oy),
                arrowprops=dict(arrowstyle="->", color="seagreen", lw=2), zorder=5)
    ax.plot([ox], [oy], "o", color="black", ms=4, zorder=6)

    ax.legend(handles=[
        Patch(facecolor="white", edgecolor="gray", label="free"),
        Patch(facecolor=(0.8, 0.8, 0.8), edgecolor="gray", label="unknown (not a wall)"),
        Patch(facecolor=(0.1, 0.1, 0.12), label="occupied"),
        Patch(facecolor=(0.85, 0.15, 0.15, 0.3), edgecolor=(0.85, 0.1, 0.1), label="extruded wall footprint"),
        Patch(facecolor=(1.0, 0.6, 0.0, 0.55), edgecolor=(0.9, 0.45, 0.0), label="doorway"),
    ], loc="upper right", fontsize=8, framealpha=0.9)
    ax.set_xlabel("x [m]  (map frame; red = +x, green = +y at map origin)")
    ax.set_ylabel("y [m]")
    ax.set_title(title, fontsize=10)
    ax.grid(True, linewidth=0.3, alpha=0.4)
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)


def render_iso(path: Path, parts: list[tuple[trimesh.Trimesh, tuple[float, float, float]]],
               bounds: np.ndarray, title: str) -> None:
    """Software isometric render (numpy + matplotlib): no GPU, display or EGL needed."""
    el, az = math.radians(32), math.radians(-55)
    d = np.array([math.cos(el) * math.cos(az), math.cos(el) * math.sin(az), math.sin(el)])
    r = np.array([-math.sin(az), math.cos(az), 0.0])
    u = np.array([-math.sin(el) * math.cos(az), -math.sin(el) * math.sin(az), math.cos(el)])
    light = 0.6 * d + 0.8 * u - 0.5 * r
    light /= np.linalg.norm(light)

    def project(p: np.ndarray) -> np.ndarray:
        return np.stack([p @ r, p @ u], axis=-1)

    polys, colors, depth = [], [], []
    for mesh, base in parts:
        tri = mesh.vertices[mesh.faces]
        visible = mesh.face_normals @ d > 1e-6
        shade = 0.40 + 0.60 * np.clip(mesh.face_normals[visible] @ light, 0.0, 1.0)
        polys.append(project(tri[visible]))
        colors.append(np.clip(np.asarray(base)[None, :] * shade[:, None], 0, 1))
        depth.append(tri[visible].mean(axis=1) @ d)
    polys, colors, depth = np.concatenate(polys), np.concatenate(colors), np.concatenate(depth)
    order = np.argsort(depth)

    (x0, y0), (x1, y1) = bounds[0, :2] - 0.5, bounds[1, :2] + 0.5
    floor = np.array([[x0, y0, 0], [x1, y0, 0], [x1, y1, 0], [x0, y1, 0]])
    span = max(x1 - x0, y1 - y0)
    step = 1.0 if span <= 30 else 2.0 if span <= 60 else 5.0
    lines = []
    for gx in np.arange(math.ceil(x0 / step) * step, x1, step):
        lines.append(project(np.array([[gx, y0, 0], [gx, y1, 0]])))
    for gy in np.arange(math.ceil(y0 / step) * step, y1, step):
        lines.append(project(np.array([[x0, gy, 0], [x1, gy, 0]])))

    fig, ax = plt.subplots(figsize=(13, 9), dpi=150)
    fig.patch.set_facecolor("#f6f7f9")
    ax.set_facecolor("#f6f7f9")
    ax.add_collection(PolyCollection([project(floor)], facecolors="#e6e9ef", edgecolors="none"))
    ax.add_collection(LineCollection(lines, colors="#cfd4dc", linewidths=0.5))
    ax.add_collection(PolyCollection(polys[order], facecolors=colors[order], edgecolors=colors[order], linewidths=0.4))
    allpts = np.concatenate([project(floor), polys.reshape(-1, 2)])
    ax.set_xlim(allpts[:, 0].min() - 0.5, allpts[:, 0].max() + 0.5)
    ax.set_ylim(allpts[:, 1].min() - 0.5, allpts[:, 1].max() + 0.5)
    ax.set_aspect("equal")
    ax.axis("off")
    fig.text(0.015, 0.975, title, va="top", fontsize=10, color="#333")
    fig.text(0.015, 0.02, f"grid = {step:g} m", fontsize=8, color="#666")
    fig.savefig(path, bbox_inches="tight", facecolor=fig.get_facecolor())
    plt.close(fig)


# ── Main ─────────────────────────────────────────────────────────────────────


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Convert a ROS occupancy map into a 3D wall model (GLB/OBJ/STL) plus previews.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--map", required=True, type=Path, help="ROS map metadata (map.yaml)")
    p.add_argument("--height", type=float, default=2.5, help="wall height [m]")
    p.add_argument("--output", type=Path, default=None,
                   help="output .glb path; .obj/.stl/previews/meta are written next to it "
                        "(default: ./<map name>_3d.glb)")
    p.add_argument("--min-feature", type=float, default=0.15,
                   help="drop isolated occupied blobs whose longest side is below this [m]")
    p.add_argument("--simplify", type=float, default=None,
                   help="outline simplification tolerance [m] (default: 1.2 map cells; lower = more faithful, more triangles)")
    p.add_argument("--gap-close", type=float, default=0.10,
                   help="bridge wall drop-outs up to this wide [m]; 0 disables")
    p.add_argument("--door-min", type=float, default=0.6, help="narrowest gap counted as a doorway [m]")
    p.add_argument("--door-max", type=float, default=1.5, help="widest gap counted as a doorway [m]")
    p.add_argument("--door-height", type=float, default=2.1,
                   help="doorway clear height [m]; a lintel fills the wall above it")
    p.add_argument("--no-doors", action="store_true", help="skip doorway detection and lintels")
    p.add_argument("--unknown-value", type=int, default=205,
                   help="8-bit pixel value that means 'unknown' (map_saver uses 205); -1 = thresholds only")
    p.add_argument("--glb-up", choices=("y", "z"), default="y",
                   help="GLB up axis: y = glTF standard (three.js/Blender), z = keep ROS axes")
    p.add_argument("--formats", default="glb,obj,stl", help="comma-separated model files to write (glb, obj, stl)")
    p.add_argument("--no-preview", action="store_true", help="skip the PNG previews")
    args = p.parse_args()
    args.formats = {f.strip().lower() for f in args.formats.split(",") if f.strip()}
    if not args.formats or not args.formats <= {"glb", "obj", "stl"}:
        p.error("--formats must be a comma-separated subset of: glb, obj, stl")
    if args.height <= 0:
        p.error("--height must be > 0")
    if args.min_feature < 0 or args.gap_close < 0:
        p.error("--min-feature and --gap-close must be >= 0")
    if not 0 < args.door_min < args.door_max:
        p.error("need 0 < --door-min < --door-max")
    return args


def main() -> int:
    args = parse_args()
    try:
        m = load_map(args.map, args.unknown_value)
    except MapError as exc:
        log(f"error: {exc}")
        return 1
    res = m.resolution

    requested = (args.output or Path(f"{args.map.stem}_3d.glb")).expanduser()
    if requested.suffix.lower() in (".glb", ".obj", ".stl"):
        requested = requested.parent / requested.stem
    base = requested  # extensions are appended, never substituted: "map_v1.2" must stay "map_v1.2"
    out_glb = base.with_name(base.name + ".glb")
    try:
        out_glb.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        log(f"error: cannot create output directory {out_glb.parent}: {exc}")
        return 1

    occ = m.occupied
    if args.gap_close > 0:
        occ = bridge_small_gaps(occ, res, args.gap_close)
    before = int(occ.sum())
    occ = remove_small_components(occ, res, args.min_feature)
    log(f"noise removal: dropped {before - int(occ.sum())} px of speckle, kept {int(occ.sum())} wall px")
    if not occ.any():
        log("error: no wall cells left after filtering; check the map or lower --min-feature")
        return 1

    simplify = args.simplify if args.simplify is not None else 1.2 * res
    local_polys = mask_to_polygons(occ, res, simplify)

    doors: list[Doorway] = []
    if not args.no_doors:
        doors = detect_doorways(occ, m.free, res, args.door_min, args.door_max)
    log(f"{len(local_polys)} wall polygon(s), {sum(len(p.exterior.coords) - 1 for p in local_polys)} outline vertices, "
        f"{len(doors)} doorway(s)")

    lintel_polys: list[Polygon] = []
    if doors and args.door_height < args.height:
        wall_union = shapely.union_all(local_polys)
        for d in doors:
            piece = d.polygon.difference(wall_union)
            lintel_polys += [g for g in getattr(piece, "geoms", [piece])
                             if isinstance(g, Polygon) and g.area > (0.5 * res) ** 2]

    walls_poly = [to_map_frame(p, m.origin) for p in local_polys]
    lintels_poly = [to_map_frame(p, m.origin) for p in lintel_polys]
    walls = extrude(walls_poly, 0.0, args.height)
    lintels = extrude(lintels_poly, args.door_height, args.height) if lintels_poly else None
    if walls is None:
        log("error: meshing produced no geometry")
        return 1

    wall_rgb, lintel_rgb = (218, 221, 228), (188, 192, 202)
    combined = trimesh.util.concatenate([x for x in (walls, lintels) if x is not None])

    # GLB (materials, optional Y-up conversion)
    if "glb" in args.formats:
        scene = trimesh.Scene()
        for name, mesh, rgb in (("walls", walls, wall_rgb), ("lintels", lintels, lintel_rgb)):
            if mesh is None:
                continue
            g = pbr(mesh, rgb)
            if args.glb_up == "y":
                g.apply_transform(ROS_TO_GLTF)
            scene.add_geometry(g, node_name=name, geom_name=name)
        out_glb.write_bytes(scene.export(file_type="glb", include_normals=False))

    # OBJ / STL: plain geometry, ROS frame, no materials
    plain = trimesh.Trimesh(vertices=combined.vertices, faces=combined.faces, process=False)
    if "obj" in args.formats:
        plain.export(base.with_name(base.name + ".obj"), file_type="obj",
                     include_normals=False, include_color=False, include_texture=False)
    if "stl" in args.formats:
        plain.export(base.with_name(base.name + ".stl"), file_type="stl")

    bounds = combined.bounds
    info = {
        "source_yaml": str(m.yaml_path),
        "source_image": str(m.image_path),
        "resolution_m_per_px": res,
        "origin_xy_yaw": list(m.origin),
        "size_px": [m.width_px, m.height_px],
        "wall_height_m": args.height,
        "door_height_m": args.door_height,
        "bounds_map_frame_m": {"min": bounds[0].tolist(), "max": bounds[1].tolist()},
        "counts": {"wall_polygons": len(walls_poly), "vertices": int(len(combined.vertices)),
                   "faces": int(len(combined.faces)), "doorways": len(doors)},
        "axes": {"obj_stl": "ROS frame: Z up, metres, origin/yaw of map.yaml",
                 "glb": "glTF Y-up: (x, z, -y) of the ROS frame" if args.glb_up == "y" else "ROS frame: Z up"},
        "doorways": [
            {"id": i, **dict(zip(("x", "y"), to_map_frame(shapely.Point(d.center), m.origin).coords[0])),
             "width_m": round(d.width, 3), "depth_m": round(d.depth, 3),
             "yaw_rad": round(d.angle + m.origin[2], 4)}
            for i, d in enumerate(doors)
        ],
    }
    base.with_name(base.name + "_meta.json").write_text(json.dumps(info, indent=2))

    if not args.no_preview:
        stats = (f"{args.map.name} | {res} m/px | wall height {args.height} m | "
                 f"{len(walls_poly)} wall polygons | {len(combined.faces)} triangles | {len(doors)} doorways")
        render_topdown(base.with_name(base.name + "_topdown.png"), m, walls_poly,
                       [Doorway(to_map_frame(d.polygon, m.origin), to_map_frame(shapely.Point(d.center), m.origin).coords[0],
                                d.width, d.depth, d.angle) for d in doors], stats)
        iso_parts = [(walls, tuple(c / 255 for c in wall_rgb))]
        if lintels is not None:
            iso_parts.append((lintels, tuple(c / 255 for c in lintel_rgb)))
        render_iso(base.with_name(base.name + "_preview.png"), iso_parts, bounds, stats)

    ext = bounds[1] - bounds[0]
    log(f"model extent {ext[0]:.2f} x {ext[1]:.2f} x {ext[2]:.2f} m, "
        f"{len(combined.vertices)} vertices, {len(combined.faces)} triangles, watertight={combined.is_watertight}")
    names = [f"{base.name}.{f}" for f in ("glb", "obj", "stl") if f in args.formats] + [f"{base.name}_meta.json"]
    if not args.no_preview:
        names += [f"{base.name}_preview.png", f"{base.name}_topdown.png"]
    log(f"wrote to {base.parent}: " + ", ".join(names))
    return 0


if __name__ == "__main__":
    sys.exit(main())
