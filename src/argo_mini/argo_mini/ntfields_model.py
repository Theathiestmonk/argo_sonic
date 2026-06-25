"""
NTFields 2D – Physics-informed neural arrival time fields for differential-drive robots.
Faithfully adapted from Liu et al., 2025 (antfields-demo/models/model_igibson.py).
Removed: igibson, renderer, occupancy grid visualisation.
Added:   CoordNormalizer for real-world SLAM map coordinates.
"""

import math
import numpy as np
import torch
import torch.nn as nn
from torch import Tensor


DIM = 2  # (x, y) C-space for a planar robot


# ── Coordinate normaliser ─────────────────────────────────────────────────────

class CoordNormalizer:
    """
    Maps real-world (x, y) metres ↔ model coordinates ∈ [-0.5, 0.5].
    Parameters come from the SLAM map YAML (origin + width/height × resolution).
    """

    def __init__(self, x_min: float, x_max: float, y_min: float, y_max: float):
        cx = (x_min + x_max) / 2.0
        cy = (y_min + y_max) / 2.0
        scale = max(x_max - x_min, y_max - y_min)
        self.offset = np.array([cx, cy], dtype=np.float32)
        self.scale  = float(scale)

    def to_model(self, xy: np.ndarray) -> np.ndarray:
        return (np.asarray(xy, dtype=np.float32) - self.offset) / self.scale

    def to_world(self, xy: np.ndarray) -> np.ndarray:
        return np.asarray(xy, dtype=np.float32) * self.scale + self.offset

    def state_dict(self) -> dict:
        return {'offset': self.offset.tolist(), 'scale': self.scale}

    @classmethod
    def from_state_dict(cls, d: dict) -> 'CoordNormalizer':
        obj = cls.__new__(cls)
        obj.offset = np.array(d['offset'], dtype=np.float32)
        obj.scale  = float(d['scale'])
        return obj


# ── Sigmoid output activation (paper §IV-B) ──────────────────────────────────

class _SigOut(nn.Module):
    def forward(self, x: Tensor) -> Tensor:
        return torch.sigmoid(0.1 * x)


# ── Neural network ─────────────────────────────────────────────────────────────

class NN(nn.Module):
    """
    τ_θ(qs, qg) = g( Φ(qs) ⊗ Φ(qg) )           (paper Eq. 10)

    Φ(q)  = f(γ(q))    where γ = random Fourier encoding, f = SIREN encoder
    ⊗       = squared-subtraction symmetric operator (Eq. 9)
    g(·)    = time-field generator (Softplus → Sigmoid)
    """

    def __init__(self, dim: int, B: Tensor, device: str = 'cpu'):
        super().__init__()
        self.dim  = dim
        self.B    = B.T.to(device)       # (dim, n_features)
        n_in = B.shape[0]               # number of Fourier features
        h    = 128

        self.scale  = 10
        self.act    = nn.Softplus(beta=self.scale)
        self.actout = _SigOut()

        self.nl1 = 3   # SIREN encoder depth
        self.nl2 = 2   # generator depth

        # C-Space Encoder – shared weights process qs and qg together
        enc = [nn.Linear(2 * n_in, h)]
        for _ in range(self.nl1 - 1):
            enc.append(nn.Linear(h, h))
        enc.append(nn.Linear(h, h))        # output projection
        self.encoder = nn.ModuleList(enc)

        # Time Field Generator
        gen = [nn.Linear(h, h) for _ in range(self.nl2)]
        gen += [nn.Linear(h, h), nn.Linear(h, 1)]
        self.generator = nn.ModuleList(gen)

    def init_weights(self, m: nn.Module):
        if isinstance(m, nn.Linear):
            stdv = 2.0 / math.sqrt(m.weight.size(1))
            m.weight.data.uniform_(-stdv, stdv)
            m.bias.data.uniform_(-stdv, stdv)

    def _fourier(self, x: Tensor) -> Tensor:
        proj = 2.0 * math.pi * (x @ self.B)
        return torch.cat([torch.sin(proj), torch.cos(proj)], dim=-1)

    def out(self, coords: Tensor):
        """
        Forward pass.
        coords : (N, 2·dim)  — [qs | qg] concatenated
        Returns: (τ, coords_with_grad)  where τ ∈ (0, 1/e)
        """
        coords = coords.clone().detach().requires_grad_(True)
        n  = coords.shape[0]
        x0 = coords[:, :self.dim]    # start points
        x1 = coords[:, self.dim:]    # goal  points

        # Shared SIREN encoding (qs and qg processed together)
        x = torch.vstack((x0, x1))
        x = self._fourier(x)
        x = torch.sin(self.encoder[0](x))
        for i in range(1, self.nl1):
            x = torch.sin(self.encoder[i](x))
        x = self.encoder[-1](x)

        # Symmetric operator ⊗ : (Φ(qs) − Φ(qg))²   (Eq. 9)
        phi_s = torch.sin(x[:n])
        phi_g = torch.sin(x[n:])
        x = (phi_s - phi_g) ** 2

        # Time Field Generator
        for i in range(self.nl2):
            x = self.act(self.generator[i](x))
        x = self.act(self.generator[-2](x))
        x = self.actout(self.generator[-1](x)) / math.e

        return x, coords        # τ, coords (grad-enabled)


# ── Model wrapper (training + inference) ──────────────────────────────────────

class NTFieldsModel:
    """
    2-D Active NTFields for differential-drive robots.

    Mode 2B offline training:
        m = NTFieldsModel(device='cuda')
        m.train_offline(points, speeds, n_epochs=3000)
        m.save('/path/to/model.pt', normalizer)

    Mode 1 inference:
        m = NTFieldsModel(device='cuda')
        norm = m.load('/path/to/model.pt')
        path_world = norm.to_world(m.predict_path(norm.to_model(start), norm.to_model(goal)))
    """

    def __init__(self, dim: int = DIM, device: str = 'cpu', lr: float = 5e-4):
        self.dim    = dim
        self.device = device
        self.B      = 0.5 * torch.randn(128, dim)

        self.network   = NN(dim, self.B, device)
        self.network.apply(self.network.init_weights)
        self.network.to(device)

        self.optimizer = torch.optim.AdamW(
            self.network.parameters(), lr=lr, weight_decay=0.1)

        self.alpha     = 1.025
        self.epoch     = 0
        self._mem_buf  = []       # online memory buffer

    # ── Internal gradient helper ───────────────────────────────────────────────

    @staticmethod
    def _autograd(y: Tensor, x: Tensor) -> Tensor:
        return torch.autograd.grad(
            y, x, torch.ones_like(y),
            only_inputs=True, retain_graph=True, create_graph=True)[0]

    # ── Speed schedule (matches original training code) ───────────────────────

    def _xform_speed(self, s: Tensor) -> Tensor:
        s = s * s * (2.0 - s) * (2.0 - s)
        return self.alpha * s + 1.0 - self.alpha

    # ── Eikonal loss (Eq. 12) ─────────────────────────────────────────────────

    def _loss(self, points: Tensor, speeds: Tensor, beta: float = 1.0):
        """
        points : (N, 2·dim)   — [qs | qg]
        speeds : (N, 2)       — [S*(qs), S*(qg)] after _xform_speed
        """
        tau, xp = self.network.out(points)
        dtau    = self._autograd(tau, xp)

        D   = xp[:, self.dim:] - xp[:, :self.dim]
        T0  = torch.einsum('ij,ij->i', D, D)
        DT0 = dtau[:, :self.dim]
        DT1 = dtau[:, self.dim:]
        T3  = tau[:, 0] ** 2
        LT  = torch.log(tau[:, 0])
        LT2 = LT ** 2;  LT3 = LT ** 3;  LT4 = LT ** 4

        T01 =  4 * LT2 * T0 / T3 * torch.einsum('ij,ij->i', DT0, DT0)
        T02 = -4 * LT3 / tau[:, 0] * torch.einsum('ij,ij->i', DT0, D)
        T11 =  4 * LT2 * T0 / T3 * torch.einsum('ij,ij->i', DT1, DT1)
        T12 =  4 * LT3 / tau[:, 0] * torch.einsum('ij,ij->i', DT1, D)

        S0 = torch.sqrt(T01 + T02 + LT4)
        S1 = torch.sqrt(T11 + T12 + LT4)

        l0 = torch.sqrt(speeds[:, 0] * S0)
        l1 = torch.sqrt(speeds[:, 1] * S1)
        loss_n = torch.sum((l0 - 1.0) ** 2 + (l1 - 1.0) ** 2) / speeds.shape[0]
        return beta * loss_n, loss_n

    # ── Offline training (Mode 2B) ─────────────────────────────────────────────

    def train_offline(self, points: Tensor, speeds: Tensor,
                      n_epochs: int = 3000, batch_size: int = 2000,
                      print_every: int = 200) -> list:
        """
        Full offline training from a static dataset.
        points : (N, 2·dim)   speeds : (N, 2)
        Returns list of per-epoch loss values.
        """
        pts = points.to(self.device)
        spd = self._xform_speed(speeds.to(self.device))
        n   = pts.shape[0]
        losses, beta = [], 1.0

        for ep in range(n_epochs):
            idx      = torch.randperm(n, device=self.device)[:batch_size]
            loss, ln = self._loss(pts[idx], spd[idx], beta)
            loss.backward()
            self.optimizer.step()
            self.optimizer.zero_grad()
            beta = 1.0 / max(ln.item(), 1e-7)
            losses.append(ln.item())
            self.epoch += 1
            if ep % print_every == 0:
                print(f"  epoch {self.epoch:>6}  loss {ln.item():.4e}")

        return losses

    # ── Online training (Mode 1.5 soft re-train) ──────────────────────────────

    def train_online(self, points: Tensor, speeds: Tensor,
                     n_steps: int = 5, batch_size: int = 1000) -> float:
        """
        Incremental update from one new observation frame.
        Called when a semi-permanent obstacle is detected (Mode 1.5).
        points : (N, 2·dim)   speeds : (N, 2)
        """
        frame = torch.cat([points, speeds], dim=1).cpu()
        self._mem_buf.append(frame)

        # Mix current frame with random past frames (paper §IV-E-2)
        parts   = [frame]
        n_past  = min(len(self._mem_buf) - 1, 19)
        if n_past > 0:
            idx = torch.randperm(len(self._mem_buf) - 1)[:n_past].tolist()
            parts += [self._mem_buf[i] for i in idx]
        batch_raw = torch.cat(parts, dim=0)

        # Cross-pair: start from one sample, goal from another (global structure)
        ps = torch.randperm(batch_raw.shape[0])
        pg = torch.randperm(batch_raw.shape[0])
        pts = torch.cat([batch_raw[ps, :self.dim],
                         batch_raw[pg, self.dim:2 * self.dim]], dim=1)
        spd = torch.cat([batch_raw[ps, 2 * self.dim:2 * self.dim + 1],
                         batch_raw[pg, 2 * self.dim + 1:]], dim=1)

        n   = min(batch_size, pts.shape[0])
        idx = torch.randperm(pts.shape[0])[:n]
        pts = pts[idx].to(self.device)
        spd = self._xform_speed(spd[idx].to(self.device))

        beta, loss_val = 1.0, 0.0
        for _ in range(n_steps):
            loss, ln = self._loss(pts, spd, beta)
            loss.backward()
            self.optimizer.step()
            self.optimizer.zero_grad()
            beta     = 1.0 / max(ln.item(), 1e-7)
            loss_val = ln.item()
            self.epoch += 1

        return loss_val

    # ── Path-following gradient (Eq. 11) ──────────────────────────────────────

    def _gradient(self, xp: Tensor) -> Tensor:
        """
        Returns bidirectional path gradient (N, 2·dim).
        Each row: [∇_qs direction,  ∇_qg direction]  both S²-normalised.
        """
        with torch.enable_grad():
            xp_d        = xp.to(self.device)
            tau, xp_g   = self.network.out(xp_d)
            dtau        = self._autograd(tau, xp_g)

            D   = xp_g[:, self.dim:] - xp_g[:, :self.dim]
            T0  = torch.sqrt(torch.einsum('ij,ij->i', D, D))
            LT  = torch.log(tau[:, 0])
            LT2 = LT ** 2
            A   = 2.0 * LT * T0 / tau[:, 0]
            B   = LT2 / T0

            g_s = -A[:, None] * dtau[:, :self.dim] + B[:, None] * D
            g_g = -A[:, None] * dtau[:, self.dim:] - B[:, None] * D
            ns  = torch.norm(g_s);  ng = torch.norm(g_g)
            g_s = g_s / (ns ** 2 + 1e-12)
            g_g = g_g / (ng ** 2 + 1e-12)
            return torch.cat([g_s, g_g], dim=1).detach().cpu()

    # ── Travel time (for NBV selection in Mode 2A) ────────────────────────────

    def travel_time(self, xp: Tensor) -> Tensor:
        """T = log(τ)² × ‖qs − qg‖   (Eq. 5)."""
        with torch.enable_grad():
            xp_d       = xp.to(self.device)
            tau, _     = self.network.out(xp_d)
            D          = xp_d[:, self.dim:] - xp_d[:, :self.dim]
            T0         = torch.einsum('ij,ij->i', D, D)
            return (torch.log(tau[:, 0]) ** 2 * torch.sqrt(T0)).detach().cpu()

    # ── Path inference ─────────────────────────────────────────────────────────

    def predict_path(self, start: np.ndarray, goal: np.ndarray,
                     step_size: float = 0.015, tol: float = 0.025,
                     max_iter: int = 400) -> np.ndarray:
        """
        Bidirectional gradient-descent path (Eq. 11).

        Coordinates must already be in model/normalised space.
        Returns (N, 2) waypoints including start and goal.
        """
        qs  = torch.tensor(start, dtype=torch.float32)
        qg  = torch.tensor(goal,  dtype=torch.float32)
        xp  = torch.cat([qs, qg]).unsqueeze(0)   # (1, 2·dim)

        pts_s, pts_g = [], []
        for _ in range(max_iter):
            g  = self._gradient(xp)
            xp = xp + step_size * g
            pts_s.append(xp[0, :self.dim].clone())
            pts_g.append(xp[0, self.dim:].clone())
            if torch.norm(xp[0, :self.dim] - xp[0, self.dim:]).item() < tol:
                break

        if not pts_s:
            return np.stack([start, goal])

        pts_g.reverse()
        all_pts = ([torch.tensor(start, dtype=torch.float32)]
                   + pts_s + pts_g
                   + [torch.tensor(goal, dtype=torch.float32)])
        return torch.stack(all_pts).numpy()

    # ── Persistence ───────────────────────────────────────────────────────────

    def save(self, path: str, normalizer: 'CoordNormalizer | None' = None):
        torch.save({
            'epoch':      self.epoch,
            'dim':        self.dim,
            'B':          self.B,
            'alpha':      self.alpha,
            'model':      self.network.state_dict(),
            'optimizer':  self.optimizer.state_dict(),
            'normalizer': normalizer.state_dict() if normalizer else None,
        }, path)

    def load(self, path: str) -> 'CoordNormalizer | None':
        ckpt = torch.load(path, map_location=self.device, weights_only=False)
        self.epoch = ckpt['epoch']
        self.dim   = ckpt['dim']
        self.B     = ckpt['B']
        self.alpha = ckpt['alpha']
        self.network = NN(self.dim, self.B, self.device)
        self.network.load_state_dict(ckpt['model'])
        self.network.to(self.device)
        self.network.eval()
        self.optimizer.load_state_dict(ckpt['optimizer'])
        nd = ckpt.get('normalizer')
        return CoordNormalizer.from_state_dict(nd) if nd else None
