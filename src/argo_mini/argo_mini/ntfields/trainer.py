"""
NTFields trainer with progressive speed scheduling.

Key ideas from C-NTFields paper
  1. Progressive β schedule  — start with uniform speed (easy), gradually
     reveal the true speed model (hard near walls).  Prevents early
     convergence to degenerate solutions where the robot drives through walls.

  2. Random mini-batch buffer — each epoch draws a fresh random subset
     from a pre-sampled pool, rather than iterating the whole dataset.
     Dramatically cuts training time vs NTFields / P-NTFields baselines.

  3. Isotropic loss  — penalises both over- and under-speed equally,
     giving a smooth, symmetric loss landscape.

  4. Mixed-precision (optional) — uses torch.cuda.amp when available
     (Jetson Orin Nano Super's Tensor Cores handle FP16 natively).
"""

from __future__ import annotations
import time
import numpy as np
import torch
import torch.optim as optim
from torch.cuda.amp import GradScaler, autocast

from .model import NTFields2D
from .speed_model import SpeedModel
from .map_utils import sample_free_positions


def _beta(epoch: int, total: int, warmup: float = 0.1) -> float:
    """
    β increases from 0 → 1 over training.

    S*_β(q) = (1 − β) + β · S*(q)
      β = 0  →  uniform speed  (trivial Eikonal, network learns global shape)
      β = 1  →  true speed model (hard near walls)

    A short warmup keeps β=0 for the first `warmup` fraction of epochs so
    the network first learns the topology of the space before the wall
    penalties are introduced.
    """
    prog = max(0.0, (epoch / total - warmup) / (1.0 - warmup + 1e-9))
    return float(min(1.0, prog))


def _isotropic_loss(
    S_pred_q0: torch.Tensor,
    S_pred_qT: torch.Tensor,
    S_star_q0: torch.Tensor,
    S_star_qT: torch.Tensor,
) -> torch.Tensor:
    """
    Eq. 10 from paper:  L = r + 1/r − 2  summed over q0 and qT,
    where r = S* / S_pred.  Minimum is 0 when S_pred == S*.
    """
    eps = 1e-6

    def _term(S_star, S_pred):
        r = (S_star + eps) / (S_pred + eps)
        return r + 1.0 / (r + eps) - 2.0

    return (_term(S_star_q0, S_pred_q0) + _term(S_star_qT, S_pred_qT)).mean()


class NTFieldsTrainer:
    """
    Trains NTFields2D on a single 2D occupancy map.

    Typical usage (on Jetson Orin Nano Super 8GB):
        trainer = NTFieldsTrainer(model, speed_model, device='cuda')
        positions = sample_free_positions(dist_field, res, ox, oy, n=50_000)
        trainer.train(positions, num_epochs=800, steps_per_epoch=150)
        model.save('/tmp/ntfields_argo.pt')
    """

    def __init__(
        self,
        model:       NTFields2D,
        speed_model: SpeedModel,
        device:      str   = 'cuda',
        lr:          float = 1e-3,
        batch_size:  int   = 512,
        eta:         float = 0.01,   # viscosity coefficient for Laplacian term
        use_amp:     bool  = True,   # mixed-precision on Jetson Tensor Cores
    ):
        self.device      = torch.device(device if torch.cuda.is_available() else 'cpu')
        self.model       = model.to(self.device)
        self.speed_model = speed_model
        self.eta         = eta
        self.batch_size  = batch_size

        self.optimizer = optim.Adam(model.parameters(), lr=lr)
        self.scheduler = optim.lr_scheduler.CosineAnnealingLR(
            self.optimizer, T_max=800, eta_min=1e-5
        )
        self.scaler = GradScaler() if (use_amp and self.device.type == 'cuda') else None

    # ── single training step ──────────────────────────────────────────────

    def _step(self, pool: np.ndarray, beta: float) -> float:
        n = len(pool)
        idx0 = np.random.randint(0, n, self.batch_size)
        idxT = np.random.randint(0, n, self.batch_size)

        q0 = torch.tensor(pool[idx0], dtype=torch.float32,
                          device=self.device).requires_grad_(True)
        qT = torch.tensor(pool[idxT], dtype=torch.float32,
                          device=self.device).requires_grad_(True)

        # Expert speed at sample positions
        S_star_q0 = self.speed_model.query_tensor(q0, self.device)
        S_star_qT = self.speed_model.query_tensor(qT, self.device)

        # Progressive scheduling: S*_β = (1-β) + β·S*
        S_star_q0_b = (1.0 - beta) + beta * S_star_q0
        S_star_qT_b = (1.0 - beta) + beta * S_star_qT

        def _compute_loss():
            S_pred_q0, S_pred_qT = self.model.predict_speed(q0, qT, eta=self.eta)
            return _isotropic_loss(S_pred_q0, S_pred_qT,
                                   S_star_q0_b, S_star_qT_b)

        self.optimizer.zero_grad()

        if self.scaler is not None:
            with autocast():
                loss = _compute_loss()
            self.scaler.scale(loss).backward()
            self.scaler.unscale_(self.optimizer)
            torch.nn.utils.clip_grad_norm_(self.model.parameters(), 1.0)
            self.scaler.step(self.optimizer)
            self.scaler.update()
        else:
            loss = _compute_loss()
            loss.backward()
            torch.nn.utils.clip_grad_norm_(self.model.parameters(), 1.0)
            self.optimizer.step()

        return float(loss.detach())

    # ── full training loop ────────────────────────────────────────────────

    def train(
        self,
        free_positions:    np.ndarray,
        num_epochs:        int   = 800,
        steps_per_epoch:   int   = 150,
        log_interval:      int   = 50,
        save_path:         str | None = None,
        progress_callback  = None,   # fn(epoch, loss, beta, elapsed_s) → None
    ) -> list[float]:
        """
        Train the model.

        Parameters
        ----------
        free_positions  : (N, 2) world-frame positions in free space
        num_epochs      : total training epochs
        steps_per_epoch : mini-batch steps per epoch
        log_interval    : print every N epochs
        save_path       : if set, save checkpoint here when done
        progress_callback : called every log_interval epochs for ROS logging
        """
        self.model.train()
        epoch_losses: list[float] = []
        t0 = time.time()

        for epoch in range(num_epochs):
            beta        = _beta(epoch, num_epochs)
            epoch_loss  = 0.0

            for _ in range(steps_per_epoch):
                epoch_loss += self._step(free_positions, beta)

            epoch_loss /= steps_per_epoch
            epoch_losses.append(epoch_loss)
            self.scheduler.step()

            if epoch % log_interval == 0:
                elapsed = time.time() - t0
                eta_s   = (elapsed / (epoch + 1)) * (num_epochs - epoch - 1)
                msg = (f'[NTFields] epoch {epoch:4d}/{num_epochs}  '
                       f'β={beta:.3f}  loss={epoch_loss:.4f}  '
                       f'elapsed={elapsed:.0f}s  ETA={eta_s:.0f}s')
                print(msg, flush=True)
                if progress_callback:
                    progress_callback(epoch, epoch_loss, beta, elapsed)

        if save_path:
            self.model.save(save_path)
            print(f'[NTFields] model saved → {save_path}')

        return epoch_losses

    # ── fine-tune from checkpoint ─────────────────────────────────────────

    def fine_tune(
        self,
        free_positions: np.ndarray,
        num_epochs:     int   = 200,
        steps_per_epoch: int  = 100,
        lr_scale:       float = 0.1,
        **kwargs,
    ) -> list[float]:
        """
        Fine-tune a pre-trained model on a new map.
        Uses lower LR and fewer epochs — ~5 min on Jetson.
        """
        for pg in self.optimizer.param_groups:
            pg['lr'] *= lr_scale
        return self.train(free_positions, num_epochs, steps_per_epoch, **kwargs)
