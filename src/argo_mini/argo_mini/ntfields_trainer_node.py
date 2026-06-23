"""
NTFields Trainer Node

Listens for /map updates from SLAM Toolbox.  When the map changes
significantly (or on first receipt) it trains an NTFields2D model
on the Jetson Orin's CUDA GPU in a background thread.

Topics
------
  /map  (nav_msgs/OccupancyGrid)  ← SLAM map

Published
---------
  /ntfields/status  (std_msgs/String)  ← IDLE | TRAINING | READY | ERROR

Saved artefact
--------------
  ~/ntfields_model.pt     — PyTorch checkpoint (model + config)
  ~/ntfields_meta.json    — map origin/resolution for the planner to load

The planner node watches ~/ntfields_model.pt for updates.
"""

import json
import os
import threading
import time

import numpy as np
import rclpy
from rclpy.node import Node
from nav_msgs.msg import OccupancyGrid
from std_msgs.msg import String

MODEL_PATH = os.path.expanduser('~/ntfields_model.pt')
META_PATH  = os.path.expanduser('~/ntfields_meta.json')


class NTFieldsTrainerNode(Node):

    def __init__(self):
        super().__init__('ntfields_trainer')

        # ── parameters ────────────────────────────────────────────────────
        self.declare_parameter('num_epochs',       800)
        self.declare_parameter('steps_per_epoch',  150)
        self.declare_parameter('batch_size',       512)
        self.declare_parameter('lr',               1e-3)
        self.declare_parameter('fourier_features', 256)
        self.declare_parameter('hidden_dim',       256)
        self.declare_parameter('n_sample_points',  60_000)
        self.declare_parameter('min_clearance_m',  0.12)
        self.declare_parameter('epsilon',          0.35)
        self.declare_parameter('lam',              2.0)
        self.declare_parameter('change_threshold', 0.05)   # 5 % new cells → retrain
        self.declare_parameter('device',           'cuda')

        p = self.get_parameter
        self._epochs       = p('num_epochs').value
        self._steps        = p('steps_per_epoch').value
        self._batch        = p('batch_size').value
        self._lr           = p('lr').value
        self._n_ff         = p('fourier_features').value
        self._h_dim        = p('hidden_dim').value
        self._n_pts        = p('n_sample_points').value
        self._clearance    = p('min_clearance_m').value
        self._epsilon      = p('epsilon').value
        self._lam          = p('lam').value
        self._change_thr   = p('change_threshold').value
        self._device       = p('device').value

        # ── state ─────────────────────────────────────────────────────────
        self._prev_occ:   np.ndarray | None = None
        self._training    = False
        self._train_lock  = threading.Lock()

        # ── pubs / subs ───────────────────────────────────────────────────
        self._status_pub = self.create_publisher(String, '/ntfields/status', 10)
        self._map_sub    = self.create_subscription(
            OccupancyGrid, '/map', self._map_cb, 1)

        self._publish_status('IDLE')
        self.get_logger().info('NTFields Trainer ready — waiting for /map')

    # ── map callback ──────────────────────────────────────────────────────

    def _map_cb(self, msg: OccupancyGrid):
        data = np.array(msg.data, dtype=np.int8)

        # Skip if map hasn't changed enough
        if self._prev_occ is not None:
            if data.shape == self._prev_occ.shape:
                diff_frac = np.mean(data != self._prev_occ)
                if diff_frac < self._change_thr:
                    return

        with self._train_lock:
            if self._training:
                self.get_logger().info(
                    'Map updated but training already in progress — skipping.')
                return
            self._training = True

        self._prev_occ = data.copy()
        meta = {
            'width':      msg.info.width,
            'height':     msg.info.height,
            'resolution': msg.info.resolution,
            'origin_x':   msg.info.origin.position.x,
            'origin_y':   msg.info.origin.position.y,
        }
        thread = threading.Thread(
            target=self._train_worker,
            args=(data.copy(), meta),
            daemon=True,
        )
        thread.start()

    # ── training worker (runs in background thread on GPU) ────────────────

    def _train_worker(self, data: np.ndarray, meta: dict):
        try:
            import torch
            from .ntfields import (
                NTFields2D, SpeedModel,
                occupancy_grid_to_distance_field,
                sample_free_positions,
                NTFieldsTrainer,
            )

            self._publish_status('TRAINING')
            self.get_logger().info(
                f'[NTFields] Training started  '
                f'({self._epochs} epochs × {self._steps} steps, '
                f'device={self._device})')

            # 1. Build distance field
            dist, _ = occupancy_grid_to_distance_field(
                data,
                meta['width'],
                meta['height'],
                meta['resolution'],
            )

            # 2. Build speed model
            speed = SpeedModel(epsilon=self._epsilon, lam=self._lam)
            speed.set_map(dist, meta['resolution'],
                          meta['origin_x'], meta['origin_y'])

            # 3. Sample free-space positions for training
            positions = sample_free_positions(
                dist,
                meta['resolution'],
                meta['origin_x'],
                meta['origin_y'],
                n=self._n_pts,
                min_clearance_m=self._clearance,
            )
            self.get_logger().info(
                f'[NTFields] Sampled {len(positions)} free-space positions.')

            # 4. Build model
            device = self._device if torch.cuda.is_available() else 'cpu'
            if device != self._device:
                self.get_logger().warn(
                    f'CUDA not available — training on CPU (will be slower).')

            model = NTFields2D(
                fourier_features=self._n_ff,
                hidden_dim=self._h_dim,
            )

            # Load existing checkpoint for fine-tune if available
            fine_tune = False
            if os.path.exists(MODEL_PATH):
                try:
                    model = NTFields2D.load(MODEL_PATH, device=device)
                    fine_tune = True
                    self.get_logger().info(
                        '[NTFields] Existing checkpoint found — fine-tuning.')
                except Exception:
                    self.get_logger().warn(
                        '[NTFields] Could not load checkpoint — training from scratch.')

            # 5. Train
            trainer = NTFieldsTrainer(
                model, speed,
                device=device,
                lr=self._lr,
                batch_size=self._batch,
            )

            def _cb(epoch, loss, beta, elapsed):
                self.get_logger().info(
                    f'[NTFields] epoch={epoch}  loss={loss:.4f}  '
                    f'β={beta:.3f}  elapsed={elapsed:.0f}s')
                self._publish_status(
                    f'TRAINING epoch={epoch}/{self._epochs} loss={loss:.4f}')

            if fine_tune:
                trainer.fine_tune(
                    positions,
                    num_epochs=max(200, self._epochs // 4),
                    steps_per_epoch=self._steps,
                    progress_callback=_cb,
                )
            else:
                trainer.train(
                    positions,
                    num_epochs=self._epochs,
                    steps_per_epoch=self._steps,
                    progress_callback=_cb,
                )

            # 6. Save
            model.save(MODEL_PATH)
            with open(META_PATH, 'w') as f:
                json.dump(meta, f, indent=2)

            self.get_logger().info(
                f'[NTFields] Training complete → {MODEL_PATH}')
            self._publish_status('READY')

        except Exception as e:
            self.get_logger().error(f'[NTFields] Training failed: {e}')
            self._publish_status(f'ERROR: {e}')

        finally:
            with self._train_lock:
                self._training = False

    def _publish_status(self, msg: str):
        m = String()
        m.data = msg
        self._status_pub.publish(m)


def main(args=None):
    rclpy.init(args=args)
    node = NTFieldsTrainerNode()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    main()
