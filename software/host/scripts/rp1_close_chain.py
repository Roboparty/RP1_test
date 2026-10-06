#!/usr/bin/env python3
"""RP1 ankle close-chain mapping used by the test tools.

This is a small Python port of src/inference/src/utils/decouple_rp1.3.cpp.
It exposes the joint-space ankle target [pitch, roll] to motor-space
[long_link_motor, short_link_motor] mapping needed before hardware replay.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, Sequence

import numpy as np


S_BAR = np.array([-1.0, 0.0, 0.0], dtype=np.float64)
ANKLE_FK_TOLERANCE = 1e-7
ANKLE_FK_ITERATIONS = 80
ANKLE_FK_DAMPING = 1e-6
ANKLE_FK_MAX_STEP = 0.08
ANKLE_FK_JAC_STEP = 1e-5


@dataclass(frozen=True)
class LinkParams:
    l_rod: float
    l_bar: float
    p_o_a: np.ndarray
    p_o_b: np.ndarray
    p_o_c: np.ndarray
    theta_0: float


def _links(is_left: bool) -> list[LinkParams]:
    if is_left:
        return [
            LinkParams(
                l_rod=210.636,
                l_bar=26.0,
                p_o_a=np.array([0.0, 0.0, -19.0], dtype=np.float64),
                p_o_b=np.array([-27.5, -15.0, -0.6], dtype=np.float64),
                p_o_c=np.array([-37.390, 0.0, 209.663], dtype=np.float64),
                theta_0=math.radians(-180.0),
            ),
            LinkParams(
                l_rod=139.941,
                l_bar=26.0,
                p_o_a=np.array([0.0, 0.0, -19.0], dtype=np.float64),
                p_o_b=np.array([-27.5, 15.0, -0.6], dtype=np.float64),
                p_o_c=np.array([-37.390, 0.0, 138.663], dtype=np.float64),
                theta_0=0.0,
            ),
        ]

    return [
        LinkParams(
            l_rod=210.636,
            l_bar=26.0,
            p_o_a=np.array([0.0, 0.0, -19.0], dtype=np.float64),
            p_o_b=np.array([-27.5, 15.0, -0.6], dtype=np.float64),
            p_o_c=np.array([-37.390, 0.0, 209.663], dtype=np.float64),
            theta_0=0.0,
        ),
        LinkParams(
            l_rod=139.941,
            l_bar=26.0,
            p_o_a=np.array([0.0, 0.0, -19.0], dtype=np.float64),
            p_o_b=np.array([-27.5, -15.0, -0.6], dtype=np.float64),
            p_o_c=np.array([-37.390, 0.0, 138.663], dtype=np.float64),
            theta_0=math.radians(-180.0),
        ),
    ]


def _is_left_for_limb(limb: str) -> bool:
    key = limb.strip().lower().replace("-", "_").replace(" ", "_")
    if key in ("left_leg", "left_short_leg"):
        return True
    if key in ("right_leg", "right_short_leg"):
        return False
    raise ValueError(f"close-chain mapping only applies to legs, got {limb!r}")


def ankle_joint_to_motor(ankle_pitch: float, ankle_roll: float, limb: str) -> np.ndarray:
    """Map ankle joint [pitch, roll] to the two close-chain motor angles."""

    is_left = _is_left_for_limb(limb)
    q_pitch = float(ankle_pitch)
    q_roll = float(ankle_roll)

    sp, cp = math.sin(q_pitch), math.cos(q_pitch)
    sr, cr = math.sin(q_roll), math.cos(q_roll)

    r_y = np.array(
        [
            [cp, 0.0, sp],
            [0.0, 1.0, 0.0],
            [-sp, 0.0, cp],
        ],
        dtype=np.float64,
    )
    r_x = np.array(
        [
            [1.0, 0.0, 0.0],
            [0.0, cr, -sr],
            [0.0, sr, cr],
        ],
        dtype=np.float64,
    )

    theta = np.zeros(2, dtype=np.float64)
    for idx, link in enumerate(_links(is_left)):
        diff_ab = link.p_o_b - link.p_o_a
        b_rot = r_y @ link.p_o_a + r_y @ r_x @ diff_ab
        diff = link.p_o_c - b_rot

        a_val = 2.0 * link.l_bar * diff[1]
        b_val = 2.0 * S_BAR[0] * link.l_bar * diff[2]
        c_val = float(diff @ diff + link.l_bar * link.l_bar - link.l_rod * link.l_rod)

        radius = math.sqrt(a_val * a_val + b_val * b_val)
        if radius <= 1e-12:
            raise ValueError("ankle close-chain equation is singular")

        cos_arg = -c_val / radius
        if abs(cos_arg) > 1.0:
            cos_arg = max(-1.0, min(1.0, cos_arg))

        phi = math.atan2(b_val, a_val)
        delta = math.acos(cos_arg)
        theta1 = phi + delta - link.theta_0
        theta2 = phi - delta - link.theta_0
        theta[idx] = theta1 if abs(theta1) < abs(theta2) else theta2

    return theta


def _ankle_fk_once(
    target: np.ndarray,
    limb: str,
    joint: np.ndarray,
    *,
    max_iterations: int,
    tolerance: float,
) -> tuple[np.ndarray | None, float]:
    """Run one damped Newton solve. Returns (joint, 0) on success, else (None, residual)."""
    joint = joint.copy()
    for _ in range(max_iterations):
        current = ankle_joint_to_motor(float(joint[0]), float(joint[1]), limb)
        error = target - current
        error_norm = float(np.linalg.norm(error))
        if error_norm < tolerance:
            return joint, 0.0

        jac = np.zeros((2, 2), dtype=np.float64)
        for col in range(2):
            perturbed = joint.copy()
            perturbed[col] += ANKLE_FK_JAC_STEP
            jac[:, col] = (
                ankle_joint_to_motor(float(perturbed[0]), float(perturbed[1]), limb) - current
            ) / ANKLE_FK_JAC_STEP

        lhs = jac.T @ jac + (ANKLE_FK_DAMPING * ANKLE_FK_DAMPING) * np.eye(2)
        rhs = jac.T @ error
        step = np.linalg.solve(lhs, rhs)
        max_abs = float(np.max(np.abs(step)))
        if max_abs > ANKLE_FK_MAX_STEP:
            step *= ANKLE_FK_MAX_STEP / max_abs
        joint = joint + step

    final = ankle_joint_to_motor(float(joint[0]), float(joint[1]), limb)
    return None, float(np.linalg.norm(target - final))


def ankle_motor_to_joint(
    long_link_motor: float,
    short_link_motor: float,
    limb: str,
    *,
    initial: Iterable[float] | None = None,
    max_iterations: int = ANKLE_FK_ITERATIONS,
    tolerance: float = ANKLE_FK_TOLERANCE,
) -> np.ndarray:
    """Solve ankle joint [pitch, roll] from close-chain motor angles."""

    target = np.asarray([long_link_motor, short_link_motor], dtype=np.float64)
    if not np.all(np.isfinite(target)):
        raise ValueError("close-chain motor target contains NaN or inf")

    seeds: list[np.ndarray] = []
    if initial is not None:
        seed = np.asarray(list(initial), dtype=np.float64)
        if seed.shape != (2,) or not np.all(np.isfinite(seed)):
            raise ValueError(f"ankle FK initial guess must contain 2 finite values, got shape {seed.shape}")
        seeds.append(seed)
    # Stand-ish and neutral seeds help when feedback is far from the last solution.
    for pitch, roll in (
        (-0.2, 0.0),
        (0.0, 0.0),
        (0.2, 0.0),
        (-0.4, 0.0),
        (-0.2, 0.2),
        (-0.2, -0.2),
        (0.0, 0.3),
        (0.0, -0.3),
    ):
        candidate = np.asarray([pitch, roll], dtype=np.float64)
        if not any(np.allclose(candidate, existing) for existing in seeds):
            seeds.append(candidate)

    best_residual = float("inf")
    for seed in seeds:
        solved, residual = _ankle_fk_once(
            target,
            limb,
            seed,
            max_iterations=max_iterations,
            tolerance=tolerance,
        )
        if solved is not None:
            return solved
        best_residual = min(best_residual, residual)

    raise ValueError(f"ankle FK did not converge for {limb}; residual={best_residual:.6g}")


def _close_chain_indices(
    indices: Sequence[int],
    *,
    motor_count: int = 6,
) -> tuple[int, int]:
    if len(indices) != 2:
        raise ValueError(f"close_chain_motor_idx must contain 2 values, got {len(indices)}")
    first, second = (int(indices[0]), int(indices[1]))
    if first == second:
        raise ValueError("close_chain_motor_idx values must be different")
    if motor_count < 2:
        raise ValueError(f"motor_count must be >= 2, got {motor_count}")
    if not 0 <= first < motor_count or not 0 <= second < motor_count:
        raise ValueError(
            f"close_chain_motor_idx values must be in [0, {motor_count - 1}], "
            f"got {list(indices)}"
        )
    return first, second


def leg_joint_to_motor(
    joint_pos: Iterable[float],
    limb: str,
    close_chain_motor_idx: Sequence[int] | None = None,
) -> np.ndarray:
    """Convert a 5/6-DoF leg joint vector to the matching motor vector.

    6-DoF order: hip_pitch, hip_roll, hip_yaw, knee, ankle_pitch, ankle_roll.
    5-DoF short-leg order: hip_roll, hip_yaw, knee, ankle_pitch, ankle_roll.
    Ankle joints are always the last two entries. The two close-chain motor
    angles are written to close_chain_motor_idx in [long-link, short-link] order.
    """

    values = np.asarray(list(joint_pos), dtype=np.float64)
    n = int(values.shape[0])
    if n not in (5, 6):
        raise ValueError(
            f"leg joint vector must have 5 or 6 values, got shape {values.shape}"
        )

    if close_chain_motor_idx is None:
        close_chain_motor_idx = (n - 2, n - 1)
    close_chain_idx = _close_chain_indices(close_chain_motor_idx, motor_count=n)
    motor = values.copy()
    motor[list(close_chain_idx)] = ankle_joint_to_motor(
        values[n - 2], values[n - 1], limb
    )
    return motor


def main() -> int:
    for limb in ("right_leg", "left_leg"):
        motor = leg_joint_to_motor([0.0, 0.0, 0.0, 0.0, -0.2, 0.0], limb)
        print(f"{limb}: {motor}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
