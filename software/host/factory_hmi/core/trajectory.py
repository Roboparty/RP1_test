"""NPZ trajectory import, integrity metadata, preview, and safety preflight."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import numpy as np

from factory_hmi.models import PreflightReport, SafetyLimits, TrajectoryData

from ._legacy import (
    canonical_limb,
    limb_motor_to_joint,
    load_motor_position_npz,
)
from .config import require_supported_limb


class TrajectoryValidationError(ValueError):
    def __init__(self, message: str, violations: list[str] | tuple[str, ...] = ()) -> None:
        self.violations = tuple(violations) or (message,)
        super().__init__(message)


def file_sha256(path: Path | str) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _preview(values: np.ndarray, timestamps: np.ndarray) -> dict[str, Any]:
    indices = sorted({0, values.shape[0] // 2, values.shape[0] - 1})
    points = [
        {
            "frame": int(index),
            "time_s": float(timestamps[index]),
            "motor_pos": [float(item) for item in values[index]],
        }
        for index in indices
    ]
    return {
        "points": points,
        "per_motor_min_rad": [float(item) for item in np.min(values, axis=0)],
        "per_motor_max_rad": [float(item) for item in np.max(values, axis=0)],
    }


def import_trajectory(
    path: Path | str,
    *,
    limb: str,
    motor_count: int,
) -> TrajectoryData:
    """Load an NPZ using ``rp1_limb_common`` and validate its schema."""

    trajectory_path = Path(path).expanduser().resolve()
    expected_limb = require_supported_limb(limb)
    try:
        motor_pos, fps, stored_limb, timestamps = load_motor_position_npz(
            trajectory_path
        )
    except (OSError, ValueError) as exc:
        raise TrajectoryValidationError(str(exc)) from exc

    if motor_pos.shape[0] == 0:
        raise TrajectoryValidationError("trajectory is empty")
    if motor_pos.shape[1] != int(motor_count):
        raise TrajectoryValidationError(
            f"motor_pos has {motor_pos.shape[1]} columns, expected {motor_count} for {expected_limb}"
        )
    if stored_limb is not None:
        try:
            actual_limb = canonical_limb(stored_limb)
        except ValueError as exc:
            raise TrajectoryValidationError(str(exc)) from exc
        if actual_limb != expected_limb:
            raise TrajectoryValidationError(
                f"trajectory limb is {actual_limb}, expected {expected_limb}"
            )

    return TrajectoryData(
        path=trajectory_path,
        limb=expected_limb,
        motor_pos=np.asarray(motor_pos, dtype=np.float64).copy(),
        timestamps=np.asarray(timestamps, dtype=np.float64).copy(),
        fps=float(fps),
        sha256=file_sha256(trajectory_path),
        preview=_preview(motor_pos, timestamps),
    )


def _limit_vector(
    value: float | list[float] | tuple[float, ...] | np.ndarray | None,
    size: int,
    name: str,
) -> np.ndarray | None:
    if value is None:
        return None
    array = np.asarray(value, dtype=np.float64)
    if array.ndim == 0:
        array = np.full(size, float(array), dtype=np.float64)
    else:
        array = array.reshape(-1)
    if array.shape != (size,):
        raise ValueError(f"{name} must be a scalar or contain {size} values")
    if not np.all(np.isfinite(array)):
        raise ValueError(f"{name} contains NaN or inf")
    return array


def _record_bound_violations(
    violations: list[str],
    values: np.ndarray,
    lower: np.ndarray | None,
    upper: np.ndarray | None,
    label: str,
) -> None:
    if lower is not None and upper is not None and np.any(lower > upper):
        violations.append(f"{label} lower bound exceeds upper bound")
        return
    if lower is not None:
        where = np.argwhere(values < lower.reshape(1, -1))
        if where.size:
            frame, index = (int(item) for item in where[0])
            violations.append(
                f"{label}[{index}] below minimum at frame {frame}: "
                f"{values[frame, index]:.6g} < {lower[index]:.6g}"
            )
    if upper is not None:
        where = np.argwhere(values > upper.reshape(1, -1))
        if where.size:
            frame, index = (int(item) for item in where[0])
            violations.append(
                f"{label}[{index}] above maximum at frame {frame}: "
                f"{values[frame, index]:.6g} > {upper[index]:.6g}"
            )


def preflight_trajectory(
    trajectory: TrajectoryData,
    *,
    entries: list[Any] | tuple[Any, ...],
    config: dict[str, Any],
    limits: SafetyLimits | None = None,
    current_motor_pos: np.ndarray | list[float] | None = None,
) -> PreflightReport:
    limits = limits or SafetyLimits.from_config(config)
    count = len(entries)
    values = np.asarray(trajectory.motor_pos, dtype=np.float64)
    timestamps = np.asarray(trajectory.timestamps, dtype=np.float64)
    violations: list[str] = []
    warnings: list[str] = []

    if values.ndim != 2 or values.shape[1] != count or values.shape[0] == 0:
        raise TrajectoryValidationError(
            f"trajectory must have shape (frames, {count}), got {values.shape}"
        )
    if not np.all(np.isfinite(values)):
        raise TrajectoryValidationError("trajectory contains NaN or inf")
    if timestamps.shape != (values.shape[0],):
        raise TrajectoryValidationError(
            f"time must contain {values.shape[0]} values, got {timestamps.size}"
        )
    if not np.all(np.isfinite(timestamps)):
        raise TrajectoryValidationError("time contains NaN or inf")
    delta_time = np.diff(timestamps)
    if delta_time.size and np.any(delta_time <= 0.0):
        raise TrajectoryValidationError("time must be strictly increasing")

    motor_min = _limit_vector(limits.motor_min_rad, count, "motor_min_rad")
    motor_max = _limit_vector(limits.motor_max_rad, count, "motor_max_rad")
    _record_bound_violations(violations, values, motor_min, motor_max, "motor")

    joint_min = _limit_vector(limits.joint_min_rad, count, "joint_min_rad")
    joint_max = _limit_vector(limits.joint_max_rad, count, "joint_max_rad")
    if joint_min is not None or joint_max is not None:
        try:
            joint_values = limb_motor_to_joint(values, config, trajectory.limb)
        except ValueError as exc:
            violations.append(f"motor-to-joint conversion failed: {exc}")
        else:
            _record_bound_violations(
                violations, joint_values, joint_min, joint_max, "joint"
            )

    frame_deltas = np.abs(np.diff(values, axis=0))
    max_frame = float(np.max(frame_deltas)) if frame_deltas.size else 0.0
    frame_limit = _limit_vector(
        limits.max_frame_delta_rad, count, "max_frame_delta_rad"
    )
    if frame_limit is not None and frame_deltas.size:
        where = np.argwhere(frame_deltas > frame_limit.reshape(1, -1))
        if where.size:
            frame, index = (int(item) for item in where[0])
            violations.append(
                f"motor[{index}] frame delta too large between frames {frame}/{frame + 1}: "
                f"{frame_deltas[frame, index]:.6g} > {frame_limit[index]:.6g} rad"
            )

    velocities = (
        frame_deltas / delta_time.reshape(-1, 1)
        if frame_deltas.size
        else np.empty((0, count), dtype=np.float64)
    )
    max_velocity = float(np.max(velocities)) if velocities.size else 0.0
    velocity_limit = _limit_vector(
        limits.max_velocity_rad_s, count, "max_velocity_rad_s"
    )
    if velocity_limit is not None and velocities.size:
        where = np.argwhere(velocities > velocity_limit.reshape(1, -1))
        if where.size:
            frame, index = (int(item) for item in where[0])
            violations.append(
                f"motor[{index}] velocity too large between frames {frame}/{frame + 1}: "
                f"{velocities[frame, index]:.6g} > {velocity_limit[index]:.6g} rad/s"
            )

    first_delta_max: float | None = None
    first_limit = _limit_vector(
        limits.max_first_frame_delta_rad,
        count,
        "max_first_frame_delta_rad",
    )
    if current_motor_pos is not None:
        current = np.asarray(current_motor_pos, dtype=np.float64).reshape(-1)
        if current.shape != (count,) or not np.all(np.isfinite(current)):
            violations.append(
                f"current motor position must contain {count} finite values"
            )
        else:
            first_delta = np.abs(values[0] - current)
            first_delta_max = float(np.max(first_delta))
            if first_limit is not None:
                where = np.flatnonzero(first_delta > first_limit)
                if where.size:
                    index = int(where[0])
                    violations.append(
                        f"motor[{index}] first-frame delta too large: "
                        f"{first_delta[index]:.6g} > {first_limit[index]:.6g} rad"
                    )
    elif first_limit is not None:
        warnings.append(
            "first-frame delta check is deferred until live motor feedback is available"
        )

    report = PreflightReport(
        safe=not violations,
        motor_count=count,
        frame_count=int(values.shape[0]),
        max_frame_delta_rad=max_frame,
        max_velocity_rad_s=max_velocity,
        max_first_frame_delta_rad=first_delta_max,
        warnings=tuple(warnings),
    )
    trajectory.preflight = report
    if violations:
        raise TrajectoryValidationError(
            "trajectory safety preflight failed: " + "; ".join(violations),
            violations,
        )
    return report


def load_and_preflight_trajectory(
    path: Path | str,
    *,
    limb: str,
    entries: list[Any] | tuple[Any, ...],
    config: dict[str, Any],
    limits: SafetyLimits | None = None,
    current_motor_pos: np.ndarray | list[float] | None = None,
) -> TrajectoryData:
    trajectory = import_trajectory(path, limb=limb, motor_count=len(entries))
    preflight_trajectory(
        trajectory,
        entries=entries,
        config=config,
        limits=limits,
        current_motor_pos=current_motor_pos,
    )
    return trajectory


__all__ = [
    "TrajectoryValidationError",
    "file_sha256",
    "import_trajectory",
    "load_and_preflight_trajectory",
    "preflight_trajectory",
]
