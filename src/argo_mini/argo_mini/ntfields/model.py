"""
NTFields 2D — Neural Time Fields for mobile robot navigation.

Architecture from: Physics-informed Neural Motion Planning on Constraint Manifolds
(Ni & Qureshi, 2024).  Simplified for 2D (x, y) configuration space.

Key properties:
  - Softplus activations throughout → C² smooth → Eikonal gradients are well-defined
  - Fourier encoder → captures high-frequency structure in the time field
  - Symmetric operator [max, min] → T(q0,qT) == T(qT,q0) by construction
  - τ output is strictly positive → T = dist/τ is well-defined
"""

import math
import torch
import torch.nn as nn


# ── building blocks ───────────────────────────────────────────────────────────

class ResBlock(nn.Module):
    def __init__(self, dim: int):
        super().__init__()
        self.fc1 = nn.Linear(dim, dim)
        self.fc2 = nn.Linear(dim, dim)
        self.act = nn.Softplus()

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.act(x + self.fc2(self.act(self.fc1(x))))


class FourierEncoder(nn.Module):
    """Random Fourier features — fixed random projection, not learned."""
    def __init__(self, input_dim: int, num_features: int, scale: float = 1.0):
        super().__init__()
        B = torch.randn(input_dim, num_features) * scale
        self.register_buffer('B', B)

    @property
    def out_dim(self) -> int:
        return 2 * self.B.shape[1]

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        proj = 2.0 * math.pi * (x @ self.B)
        return torch.cat([torch.cos(proj), torch.sin(proj)], dim=-1)


class Encoder(nn.Module):
    """γ: Fourier features → latent embedding."""
    def __init__(self, in_dim: int, hidden: int, n_blocks: int):
        super().__init__()
        self.proj = nn.Sequential(nn.Linear(in_dim, hidden), nn.Softplus())
        self.blocks = nn.ModuleList([ResBlock(hidden) for _ in range(n_blocks)])

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        x = self.proj(x)
        for blk in self.blocks:
            x = blk(x)
        return x


class Decoder(nn.Module):
    """g: symmetric embedding → factorised time τ (strictly positive)."""
    def __init__(self, in_dim: int, hidden: int, n_blocks: int):
        super().__init__()
        self.proj = nn.Sequential(nn.Linear(in_dim, hidden), nn.Softplus())
        self.blocks = nn.ModuleList([ResBlock(hidden) for _ in range(n_blocks)])
        self.head = nn.Linear(hidden, 1)
        self.out_act = nn.Softplus()

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        x = self.proj(x)
        for blk in self.blocks:
            x = blk(x)
        return self.out_act(self.head(x)).squeeze(-1) + 1e-6   # τ > 0


# ── main model ────────────────────────────────────────────────────────────────

class NTFields2D(nn.Module):
    """
    NTFields for 2D navigation.

    Inputs:  q0, qT  — (B, 2) tensors in world-metric coordinates (metres)
    Output:  τ       — (B,)   factorised arrival time

    Arrival time:  T(q0, qT) = ‖q0 − qT‖ / τ(q0, qT)
    Speed:         S(qT)      = 1 / ‖∇_{qT} T‖        (Eikonal equation)

    The symmetric operator ensures T(A→B) == T(B→A).
    """

    def __init__(
        self,
        fourier_features: int = 256,
        fourier_scale:    float = 1.0,
        hidden_dim:       int   = 256,
        encoder_blocks:   int   = 3,
        decoder_blocks:   int   = 3,
    ):
        super().__init__()
        self.fourier  = FourierEncoder(2, fourier_features, fourier_scale)
        f_out         = self.fourier.out_dim
        self.encoder  = Encoder(f_out, hidden_dim, encoder_blocks)
        self.decoder  = Decoder(2 * hidden_dim, hidden_dim, decoder_blocks)

    # -- forward ---------------------------------------------------------------

    def _embed(self, q: torch.Tensor) -> torch.Tensor:
        return self.encoder(self.fourier(q))

    def forward(self, q0: torch.Tensor, qT: torch.Tensor) -> torch.Tensor:
        """Return τ(q0, qT)."""
        h0, hT = self._embed(q0), self._embed(qT)
        sym = torch.cat([torch.max(h0, hT), torch.min(h0, hT)], dim=-1)
        return self.decoder(sym)

    def arrival_time(self, q0: torch.Tensor, qT: torch.Tensor) -> torch.Tensor:
        """T = ‖q0 − qT‖ / τ."""
        dist = torch.norm(q0 - qT, dim=-1).clamp(min=1e-6)
        return dist / self.forward(q0, qT)

    # -- speed prediction (used at inference & loss) ---------------------------

    def predict_speed(
        self,
        q0:  torch.Tensor,
        qT:  torch.Tensor,
        eta: float = 0.01,
    ) -> tuple[torch.Tensor, torch.Tensor]:
        """
        Return predicted speed at q0 and qT via the viscosity Eikonal equation.

        S(qT) = 1 / (η·Δ_{qT}τ + ‖∇_{qT}T‖)

        The Laplacian term η·Δτ regularises the solution near sharp speed
        boundaries (thin walls), preventing the ill-posed Eikonal collapse.
        """
        q0 = q0.requires_grad_(True)
        qT = qT.requires_grad_(True)

        tau  = self.forward(q0, qT)
        dist = torch.norm(q0 - qT, dim=-1).clamp(min=1e-6)
        T    = dist / tau

        def _speed_at(q_target: torch.Tensor, T_val: torch.Tensor):
            grad = torch.autograd.grad(
                T_val.sum(), q_target, create_graph=True
            )[0]
            lap = sum(
                torch.autograd.grad(
                    grad[:, i].sum(), q_target, create_graph=True
                )[0][:, i]
                for i in range(2)
            )
            return 1.0 / (torch.norm(grad, dim=-1).clamp(min=1e-6)
                          + eta * lap + 1e-6)

        S_qT = _speed_at(qT, T)

        # Speed at q0: swap roles (arrival-time symmetry)
        tau2 = self.forward(qT, q0)
        T2   = dist / tau2
        S_q0 = _speed_at(q0, T2)

        return S_q0, S_qT

    # -- utilities -------------------------------------------------------------

    def num_params(self) -> int:
        return sum(p.numel() for p in self.parameters() if p.requires_grad)

    def save(self, path: str):
        torch.save({'state_dict': self.state_dict(),
                    'config': self._config()}, path)

    def _config(self) -> dict:
        return {
            'fourier_features': self.fourier.B.shape[1],
            'fourier_scale':    1.0,
            'hidden_dim':       self.encoder.proj[0].out_features,
            'encoder_blocks':   len(self.encoder.blocks),
            'decoder_blocks':   len(self.decoder.blocks),
        }

    @classmethod
    def load(cls, path: str, device: str = 'cpu') -> 'NTFields2D':
        ckpt = torch.load(path, map_location=device)
        model = cls(**ckpt['config'])
        model.load_state_dict(ckpt['state_dict'])
        return model.to(device)
