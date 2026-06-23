"""
Expert speed model  S*(x, y, t)  for restaurant / hotel navigation.

Composed of two layers:

  S_static(x,y)    — built once from the SLAM occupancy map.
                     Encodes wall clearance: open corridors = fast,
                     near-wall / near-table = slow.

  S_social(x,y,t)  — updated in real-time from detected human positions.
                     Each person creates a Gaussian personal-space slow zone.

Final speed:  S*(x,y,t) = S_static(x,y) × S_social(x,y,t)

Both components are in [0, 1].  The Eikonal trainer uses S* as ground truth;
the navigator uses S* to scale cmd_vel at run-time.
"""

from __future__ import annotations
import math
import numpy as np
import torch


# ── static (map-based) component ─────────────────────────────────────────────

class StaticSpeedModel:
    """
    S_static(x,y)  from the occupancy-grid distance field.

    Uses the C-NTFields exponential formulation (Eq. 5):

        d(x,y)        = max(0,  ε − dist_to_obstacle(x,y))
        S_static(x,y) = exp( −d²  /  (λ · ε²) )

    At obstacle surface : d = ε  →  S = exp(−1/λ)  ≈ 0.22  (with λ=2)
    At ε metres away    : d = 0  →  S = 1.0  (full speed)

    Parameters
    ----------
    epsilon  : safety horizon  (m) — recommended ≥ robot_radius + 0.10
    lam      : sharpness of decay  — larger = gentler slope, smaller = sharper
    s_const  : maximum speed normalisation  (keep 1.0 for dimensionless field)
    """

    def __init__(
        self,
        epsilon:  float = 0.35,
        lam:      float = 2.0,
        s_const:  float = 1.0,
    ):
        self.epsilon  = epsilon
        self.lam      = lam
        self.s_const  = s_const

        self._dist:   np.ndarray | None = None
        self._res:    float = 0.05
        self._ox:     float = 0.0
        self._oy:     float = 0.0
        self._h:      int   = 0
        self._w:      int   = 0

    # ── map ingestion ──────────────────────────────────────────────────────

    def set_map(
        self,
        dist_field: np.ndarray,
        resolution: float,
        origin_x:   float,
        origin_y:   float,
    ):
        self._dist = dist_field.astype(np.float32)
        self._res  = resolution
        self._ox   = origin_x
        self._oy   = origin_y
        self._h, self._w = dist_field.shape

        # Pre-compute S_static grid for fast lookup
        d      = np.maximum(0.0, self.epsilon - self._dist)
        self._S = self.s_const * np.exp(-d ** 2 / (self.lam * self.epsilon ** 2 + 1e-9))
        self._S = self._S.astype(np.float32)

    # ── lookup ─────────────────────────────────────────────────────────────

    def _to_grid(self, x: np.ndarray, y: np.ndarray):
        col = np.clip(((x - self._ox) / self._res).astype(int), 0, self._w - 1)
        row = np.clip(((y - self._oy) / self._res).astype(int), 0, self._h - 1)
        return row, col

    def query_np(self, positions: np.ndarray) -> np.ndarray:
        """(N,2) world coords → (N,) speed values [0,1]."""
        if self._S is None:
            return np.ones(len(positions), dtype=np.float32)
        r, c = self._to_grid(positions[:, 0], positions[:, 1])
        return self._S[r, c]

    def query_tensor(
        self, positions: torch.Tensor, device: str | torch.device = 'cpu'
    ) -> torch.Tensor:
        """(N,2) tensor → (N,) tensor."""
        np_pos = positions.detach().cpu().numpy()
        return torch.tensor(self.query_np(np_pos), dtype=torch.float32,
                            device=device)

    def dist_at(self, x: float, y: float) -> float:
        """Obstacle distance at a single world point (metres)."""
        if self._dist is None:
            return float('inf')
        r, c = self._to_grid(np.array([x]), np.array([y]))
        return float(self._dist[r[0], c[0]])

    @property
    def ready(self) -> bool:
        return self._dist is not None


# ── dynamic (social / human) component ───────────────────────────────────────

class SocialSpeedModel:
    """
    S_social(x,y,t)  from detected human positions.

    Each detected person at (hx, hy) creates an inverse-Gaussian slow zone:

        S_person(x,y) = 1 − A · exp(−‖(x,y)−(hx,hy)‖² / (2σ²))

    where
      A  = avoidance amplitude  (0 < A ≤ 1):  1 = full stop at person centre
      σ  = personal space radius (m)

    Multiple people are composed multiplicatively:
        S_social = ∏_i  S_person_i

    The multiplicative form ensures:
      - One person very close  → strongly slows the robot
      - Many people far away   → mild combined effect
      - No people              → S_social = 1.0 (no effect)

    Tuning for restaurant / hotel:
      sigma = 0.7 m  (tight personal space — tables are close)
      A     = 0.95   (almost stop at person's centre, not quite 0 to allow
                       recovery from bad detections)
    """

    def __init__(
        self,
        sigma:     float = 0.7,
        amplitude: float = 0.95,
        max_range: float = 2.0,   # beyond this distance, person has no effect
    ):
        self.sigma     = sigma
        self.amplitude = amplitude
        self.max_range = max_range
        self._humans:  list[tuple[float, float]] = []

    def update_humans(self, positions: list[tuple[float, float]]):
        """Update list of (x, y) human positions in world frame."""
        self._humans = list(positions)

    def query_np(self, positions: np.ndarray) -> np.ndarray:
        """(N,2) world coords → (N,) combined social speed [0,1]."""
        S = np.ones(len(positions), dtype=np.float32)
        for hx, hy in self._humans:
            dx = positions[:, 0] - hx
            dy = positions[:, 1] - hy
            d2 = dx ** 2 + dy ** 2
            within = d2 < self.max_range ** 2
            # Inverse Gaussian: S = 1 - A·exp(-d²/2σ²)
            person_S = 1.0 - self.amplitude * np.exp(
                -d2 / (2.0 * self.sigma ** 2 + 1e-9)
            )
            S = np.where(within, S * person_S, S)
        return S.astype(np.float32)

    def query_tensor(
        self, positions: torch.Tensor, device: str | torch.device = 'cpu'
    ) -> torch.Tensor:
        return torch.tensor(
            self.query_np(positions.detach().cpu().numpy()),
            dtype=torch.float32, device=device
        )

    @property
    def human_count(self) -> int:
        return len(self._humans)


# ── combined model ────────────────────────────────────────────────────────────

class SpeedModel:
    """
    Full speed model  S*(x,y,t) = S_static(x,y) × S_social(x,y,t).

    This is what the NTFields trainer optimises against and what the
    social shield uses to scale cmd_vel at run-time.
    """

    def __init__(
        self,
        epsilon:   float = 0.35,
        lam:       float = 2.0,
        sigma:     float = 0.7,
        amplitude: float = 0.95,
    ):
        self.static = StaticSpeedModel(epsilon=epsilon, lam=lam)
        self.social = SocialSpeedModel(sigma=sigma, amplitude=amplitude)

    def set_map(self, dist_field, resolution, origin_x, origin_y):
        self.static.set_map(dist_field, resolution, origin_x, origin_y)

    def update_humans(self, positions):
        self.social.update_humans(positions)

    def query_np(self, positions: np.ndarray) -> np.ndarray:
        S_st = self.static.query_np(positions)
        S_so = self.social.query_np(positions)
        return (S_st * S_so).astype(np.float32)

    def query_tensor(self, positions: torch.Tensor,
                     device: str | torch.device = 'cpu') -> torch.Tensor:
        np_pos = positions.detach().cpu().numpy()
        return torch.tensor(self.query_np(np_pos), dtype=torch.float32,
                            device=device)

    def query_single(self, x: float, y: float) -> float:
        pos = np.array([[x, y]], dtype=np.float32)
        return float(self.query_np(pos)[0])

    @property
    def ready(self) -> bool:
        return self.static.ready
