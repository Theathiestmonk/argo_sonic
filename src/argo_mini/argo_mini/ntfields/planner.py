"""
NTFields path planner — bidirectional gradient descent on the time field.

Navigation update rule (Eq. 11, paper):
    q0 ← q0 − α · S²(q0) · ∇_{q0} T(q0, qT)
    qT ← qT − α · S²(qT) · ∇_{qT} T(q0, qT)

The two ends march toward each other through open space following the
gradient of the arrival time field.  Speed S² weighting naturally slows
the step near obstacles (low S) and accelerates in open space (high S).

Output is a smooth nav_msgs/Path — suitable for MPPI to follow directly.
"""

from __future__ import annotations
import math
import numpy as np
import torch

from .model import NTFields2D


def _heading(p1: np.ndarray, p2: np.ndarray) -> float:
    """Heading angle (rad) from p1 to p2."""
    dx, dy = p2 - p1
    return math.atan2(dy, dx)


def _yaw_to_quat(yaw: float) -> tuple[float, float, float, float]:
    """Yaw → quaternion (x, y, z, w) — z-axis rotation only."""
    return 0.0, 0.0, math.sin(yaw * 0.5), math.cos(yaw * 0.5)


def _smooth_path(pts: np.ndarray, min_dist: float = 0.05) -> np.ndarray:
    """
    Remove duplicate / too-close waypoints.
    Keeps first and last points always.
    """
    if len(pts) <= 2:
        return pts
    kept = [pts[0]]
    for p in pts[1:-1]:
        if np.linalg.norm(p - kept[-1]) >= min_dist:
            kept.append(p)
    kept.append(pts[-1])
    return np.array(kept, dtype=np.float32)


class NTFieldsPlanner:
    """
    Generates collision-free, time-optimal paths by gradient descent on τ.

    Parameters
    ----------
    model            : trained NTFields2D
    device           : 'cuda' or 'cpu'
    alpha            : gradient step size (m per step, effectively)
    goal_radius      : stop condition — ‖q0 − qT‖ < goal_radius
    max_steps        : safety cap on iterations
    waypoint_stride  : save a waypoint every N gradient steps
    min_wp_dist      : minimum distance between consecutive waypoints (m)
    """

    def __init__(
        self,
        model:          NTFields2D,
        device:         str   = 'cuda',
        alpha:          float = 0.03,
        goal_radius:    float = 0.12,
        max_steps:      int   = 600,
        waypoint_stride: int  = 4,
        min_wp_dist:    float = 0.05,
    ):
        self.model          = model.to(device).eval()
        self.device         = torch.device(device)
        self.alpha          = alpha
        self.goal_radius    = goal_radius
        self.max_steps      = max_steps
        self.waypoint_stride = waypoint_stride
        self.min_wp_dist    = min_wp_dist

    # ── core planner ──────────────────────────────────────────────────────

    @torch.no_grad()
    def _gradient_step(
        self,
        q0: torch.Tensor,
        qT: torch.Tensor,
    ) -> tuple[torch.Tensor, torch.Tensor, float, float]:
        """One bidirectional gradient step.  Returns (new_q0, new_qT, S_q0, S_qT)."""
        with torch.enable_grad():
            q0 = q0.detach().requires_grad_(True)
            qT = qT.detach().requires_grad_(True)

            tau  = self.model(q0, qT)
            dist = torch.norm(q0 - qT).clamp(min=1e-6)
            T    = dist / tau

            g_q0 = torch.autograd.grad(T, q0, retain_graph=True)[0]
            g_qT = torch.autograd.grad(T, qT)[0]

        S_q0 = (1.0 / (g_q0.norm().clamp(min=1e-6))).clamp(max=1.0)
        S_qT = (1.0 / (g_qT.norm().clamp(min=1e-6))).clamp(max=1.0)

        new_q0 = (q0 - self.alpha * S_q0 ** 2 * g_q0).detach()
        new_qT = (qT - self.alpha * S_qT ** 2 * g_qT).detach()

        return new_q0, new_qT, float(S_q0), float(S_qT)

    def plan(
        self,
        start: np.ndarray,         # (2,) world coords
        goal:  np.ndarray,         # (2,) world coords
    ) -> np.ndarray | None:
        """
        Plan path from start to goal.

        Returns
        -------
        (N, 2) float32 array of waypoints in world frame,
        or None if the planner fails to converge.
        """
        if np.linalg.norm(start - goal) < self.goal_radius:
            return np.stack([start, goal]).astype(np.float32)

        q0 = torch.tensor(start, dtype=torch.float32,
                          device=self.device).unsqueeze(0)
        qT = torch.tensor(goal,  dtype=torch.float32,
                          device=self.device).unsqueeze(0)

        fwd_pts:  list[np.ndarray] = [start.copy()]
        bwd_pts:  list[np.ndarray] = [goal.copy()]

        for step in range(self.max_steps):
            dist = torch.norm(q0 - qT).item()
            if dist < self.goal_radius:
                break

            q0, qT, _, _ = self._gradient_step(q0, qT)

            if step % self.waypoint_stride == 0:
                fwd_pts.append(q0[0].cpu().numpy())
                bwd_pts.append(qT[0].cpu().numpy())
        else:
            # Did not converge — return best effort
            pass

        # Join forward and backward halves
        path = np.array(fwd_pts + list(reversed(bwd_pts)),
                        dtype=np.float32)
        return _smooth_path(path, self.min_wp_dist)

    # ── nav_msgs/Path builder (ROS2) ──────────────────────────────────────

    def plan_as_ros_path(
        self,
        start:     np.ndarray,
        goal:      np.ndarray,
        frame_id:  str,
        stamp,                 # rclpy Time object
    ):
        """
        Plan and return a nav_msgs.msg.Path.
        Import nav_msgs only here so the core planner stays ROS-free.
        """
        from geometry_msgs.msg import PoseStamped
        from nav_msgs.msg import Path

        waypoints = self.plan(start, goal)
        if waypoints is None or len(waypoints) < 2:
            return None

        path_msg = Path()
        path_msg.header.frame_id = frame_id
        path_msg.header.stamp    = stamp

        for i, wp in enumerate(waypoints):
            ps          = PoseStamped()
            ps.header   = path_msg.header

            ps.pose.position.x = float(wp[0])
            ps.pose.position.y = float(wp[1])
            ps.pose.position.z = 0.0

            # Heading: point toward next waypoint; last waypoint uses goal heading
            if i < len(waypoints) - 1:
                yaw = _heading(wp, waypoints[i + 1])
            else:
                yaw = _heading(waypoints[-2], waypoints[-1])

            qx, qy, qz, qw          = _yaw_to_quat(yaw)
            ps.pose.orientation.x    = qx
            ps.pose.orientation.y    = qy
            ps.pose.orientation.z    = qz
            ps.pose.orientation.w    = qw

            path_msg.poses.append(ps)

        return path_msg

    # ── utilities ─────────────────────────────────────────────────────────

    def reload_model(self, path: str):
        self.model = NTFields2D.load(path, device=str(self.device)).eval()
