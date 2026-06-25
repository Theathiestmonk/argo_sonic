#!/usr/bin/env python3
"""
Mode 2B: Offline NTFields training from a SLAM occupancy grid.

Usage:
    python3 ntfields_offline_train.py \
        --map  ~/maps/site_001.yaml \
        --output ~/ntfields_models/site_001.pt \
        [--epochs 3000] [--pairs 200000] [--batch 2000] [--device cuda]

The SLAM YAML must be a standard Nav2/SLAM Toolbox map file
(fields: image, resolution, origin, occupied_thresh, free_thresh).

The output .pt file embeds the CoordNormalizer so Mode 1 can load it directly.
"""

import argparse
import os
import sys
import time

import numpy as np
import yaml
from PIL import Image

# Locate the argo_mini package regardless of whether it is installed
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, '..', '..'))

from argo_mini.argo_mini.ntfields_model import NTFieldsModel, CoordNormalizer
from argo_mini.argo_mini.ntfields_speed import sample_training_pairs_from_map


# ── Map loading ───────────────────────────────────────────────────────────────

def load_slam_map(yaml_path: str):
    """
    Load a Nav2/SLAM Toolbox PGM map and return (occ_data, resolution, origin_xy).

    PGM pixel conventions (SLAM Toolbox):
        255 → free       → occ = 0
        0   → occupied   → occ = 100
        205 → unknown    → occ = -1
    """
    with open(yaml_path) as f:
        meta = yaml.safe_load(f)

    pgm = meta['image']
    if not os.path.isabs(pgm):
        pgm = os.path.join(os.path.dirname(yaml_path), pgm)

    img = np.array(Image.open(pgm).convert('L'))
    res = float(meta['resolution'])
    origin = meta['origin']
    origin_xy = np.array([origin[0], origin[1]], dtype=np.float32)

    occ = np.full(img.shape, -1, dtype=np.int8)
    occ[img > 220]  = 0    # free
    occ[img < 100]  = 100  # occupied

    return occ, res, origin_xy


# ── Normalizer ────────────────────────────────────────────────────────────────

def build_normalizer(occ: np.ndarray, res: float,
                     origin: np.ndarray) -> CoordNormalizer:
    H, W = occ.shape
    x_min = float(origin[0])
    x_max = float(origin[0] + W * res)
    # ROS: y_min at origin, y_max at top row
    y_min = float(origin[1])
    y_max = float(origin[1] + H * res)
    return CoordNormalizer(x_min, x_max, y_min, y_max)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser(
        description='NTFields Mode 2B offline training from SLAM map')
    p.add_argument('--map',    required=True, help='SLAM map YAML path')
    p.add_argument('--output', required=True, help='Output .pt model path')
    p.add_argument('--epochs', type=int,   default=3000)
    p.add_argument('--pairs',  type=int,   default=200_000,
                   help='Training start-goal pairs to sample from map')
    p.add_argument('--batch',  type=int,   default=2000)
    p.add_argument('--d-min',  type=float, default=0.07,
                   help='Min obstacle distance metres (speed=0)')
    p.add_argument('--d-max',  type=float, default=0.70,
                   help='Max obstacle distance metres (speed=1)')
    import torch
    p.add_argument('--device', default='cuda' if torch.cuda.is_available() else 'cpu')
    args = p.parse_args()

    print(f'[NTFields] Map:    {args.map}')
    print(f'[NTFields] Output: {args.output}')
    print(f'[NTFields] Device: {args.device}')

    occ, res, origin = load_slam_map(args.map)
    H, W = occ.shape
    n_free = int(np.sum(occ == 0))
    print(f'[NTFields] Grid {W}×{H}  res={res:.3f}m  '
          f'free={n_free} cells ({n_free * res * res:.1f} m²)')

    norm = build_normalizer(occ, res, origin)
    print(f'[NTFields] Normalizer  offset={norm.offset}  scale={norm.scale:.2f}m')

    print(f'[NTFields] Sampling {args.pairs:,} pairs via EDT…')
    t0 = time.time()
    points, speeds = sample_training_pairs_from_map(
        occ, res, origin, norm,
        n_pairs=args.pairs,
        d_min_m=args.d_min,
        d_max_m=args.d_max,
    )
    print(f'[NTFields] Dataset ready in {time.time()-t0:.1f}s  '
          f'points={tuple(points.shape)}  speeds={tuple(speeds.shape)}')

    model = NTFieldsModel(dim=2, device=args.device)
    print(f'[NTFields] Training {args.epochs} epochs…')
    t0 = time.time()
    losses = model.train_offline(
        points, speeds,
        n_epochs=args.epochs,
        batch_size=args.batch,
        print_every=200,
    )
    elapsed = time.time() - t0
    print(f'[NTFields] Done in {elapsed:.0f}s  final loss={losses[-1]:.4e}')

    out_dir = os.path.dirname(os.path.abspath(args.output))
    os.makedirs(out_dir, exist_ok=True)
    model.save(args.output, norm)
    print(f'[NTFields] Model saved → {args.output}')


if __name__ == '__main__':
    main()
