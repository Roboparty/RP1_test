"""RP1 ankle close-chain mapping, isolated from CLI and vendor SDK code."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np


@dataclass(frozen=True)
class Link:
    rod: float
    bar: float
    a: np.ndarray
    b: np.ndarray
    c: np.ndarray
    theta0: float


def _links(left: bool) -> tuple[Link, Link]:
    side = -1.0 if left else 1.0
    return (
        Link(
            210.636,
            26.0,
            np.array([0.0, 0.0, -19.0]),
            np.array([-27.5, 15.0 * side, -0.6]),
            np.array([-37.390, 0.0, 209.663]),
            math.radians(-180.0) if left else 0.0,
        ),
        Link(
            139.941,
            26.0,
            np.array([0.0, 0.0, -19.0]),
            np.array([-27.5, -15.0 * side, -0.6]),
            np.array([-37.390, 0.0, 138.663]),
            0.0 if left else math.radians(-180.0),
        ),
    )


def ankle_joint_to_motor(pitch: float, roll: float, *, left: bool) -> np.ndarray:
    sp, cp = math.sin(pitch), math.cos(pitch)
    sr, cr = math.sin(roll), math.cos(roll)
    ry = np.array([[cp, 0.0, sp], [0.0, 1.0, 0.0], [-sp, 0.0, cp]])
    rx = np.array([[1.0, 0.0, 0.0], [0.0, cr, -sr], [0.0, sr, cr]])
    result = np.zeros(2, dtype=np.float64)
    for index, link in enumerate(_links(left)):
        rotated_b = ry @ link.a + ry @ rx @ (link.b - link.a)
        delta = link.c - rotated_b
        a_value = 2.0 * link.bar * delta[1]
        b_value = -2.0 * link.bar * delta[2]
        c_value = float(delta @ delta + link.bar**2 - link.rod**2)
        radius = math.hypot(a_value, b_value)
        if radius <= 1e-12:
            raise ValueError("ankle close-chain equation is singular")
        phase = math.atan2(b_value, a_value)
        offset = math.acos(float(np.clip(-c_value / radius, -1.0, 1.0)))
        candidates = (phase + offset - link.theta0, phase - offset - link.theta0)
        result[index] = min(candidates, key=abs)
    return result


def ankle_motor_to_joint(
    motor_angles: np.ndarray, *, left: bool, initial: np.ndarray | None = None
) -> np.ndarray:
    target = np.asarray(motor_angles, dtype=np.float64).reshape(2)
    joint = (
        np.asarray(initial, dtype=np.float64).reshape(2).copy()
        if initial is not None
        else np.asarray([-0.2, 0.0], dtype=np.float64)
    )
    for _ in range(80):
        current = ankle_joint_to_motor(float(joint[0]), float(joint[1]), left=left)
        error = target - current
        if float(np.linalg.norm(error)) < 1e-7:
            return joint
        jacobian = np.empty((2, 2), dtype=np.float64)
        for column in range(2):
            shifted = joint.copy()
            shifted[column] += 1e-5
            jacobian[:, column] = (
                ankle_joint_to_motor(float(shifted[0]), float(shifted[1]), left=left)
                - current
            ) / 1e-5
        step = np.linalg.solve(
            jacobian.T @ jacobian + 1e-12 * np.eye(2), jacobian.T @ error
        )
        maximum = float(np.max(np.abs(step)))
        if maximum > 0.08:
            step *= 0.08 / maximum
        joint += step
    raise ValueError("ankle close-chain inverse did not converge")


def _motor_signs(config: dict[str, Any], count: int) -> np.ndarray:
    raw = config.get("motor_sign")
    if raw is None:
        hardware = config.get("hardware", {})
        motors = hardware.get("motors", []) if isinstance(hardware, dict) else []
        if motors:
            raw = [
                item["sign"]
                for item in sorted(motors, key=lambda value: int(value["motor_index"]))
            ]
    signs = np.asarray(raw if raw is not None else [1.0] * count, dtype=np.float64)
    if signs.shape != (count,) or np.any(signs == 0):
        raise ValueError("motor signs must contain one non-zero value per mapped motor")
    return signs


def joint_frames_to_motor(joint_frames: np.ndarray, config: dict[str, Any]) -> np.ndarray:
    """Apply configured close-chain pairs and per-motor signs."""
    result = np.asarray(joint_frames, dtype=np.float64).copy()
    mappings = config.get("close_chain", [])
    if mappings is None:
        mappings = []
    if not isinstance(mappings, list):
        raise ValueError("close_chain must be a list")
    for item in mappings:
        pitch_index = int(item["pitch_index"])
        roll_index = int(item["roll_index"])
        motor_indices = [int(value) for value in item["motor_indices"]]
        if len(motor_indices) != 2:
            raise ValueError("close_chain motor_indices must contain two indexes")
        left = str(item["side"]).lower() == "left"
        for row in result:
            row[motor_indices] = ankle_joint_to_motor(
                float(row[pitch_index]), float(row[roll_index]), left=left
            )
    signs = _motor_signs(config, result.shape[1])
    return result * signs


def motor_frames_to_joint(motor_frames: np.ndarray, config: dict[str, Any]) -> np.ndarray:
    result = np.asarray(motor_frames, dtype=np.float64).copy()
    signs = _motor_signs(config, result.shape[1])
    result /= signs
    mappings = config.get("close_chain", []) or []
    if not isinstance(mappings, list):
        raise ValueError("close_chain must be a list")
    for item in mappings:
        pitch_index = int(item["pitch_index"])
        roll_index = int(item["roll_index"])
        motor_indices = [int(value) for value in item["motor_indices"]]
        left = str(item["side"]).lower() == "left"
        initial = None
        for row in result:
            initial = ankle_motor_to_joint(
                row[motor_indices], left=left, initial=initial
            )
            row[[pitch_index, roll_index]] = initial
    return result
