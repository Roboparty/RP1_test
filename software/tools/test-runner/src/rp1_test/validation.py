"""Configuration-bound trajectory safety validation."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from .close_chain import motor_frames_to_joint
from .mapping import load_joint_mappings
from .trajectory import Trajectory, fingerprint_config, fingerprint_file


@dataclass(frozen=True)
class JointSafetyLimit:
    joint_name: str
    position_min_rad: float
    position_max_rad: float
    max_velocity_rad_s: float
    max_acceleration_rad_s2: float


def load_safety_limits(config: dict[str, Any]) -> list[JointSafetyLimit]:
    raw = config.get("joint_mappings")
    if not isinstance(raw, list):
        raise ValueError("joint_mappings must be a list")
    limits: list[JointSafetyLimit] = []
    for item in sorted(raw, key=lambda value: int(value["motor_index"])):
        try:
            limit = JointSafetyLimit(
                joint_name=str(item["joint_name"]),
                position_min_rad=float(item["position_min_rad"]),
                position_max_rad=float(item["position_max_rad"]),
                max_velocity_rad_s=float(item["max_velocity_rad_s"]),
                max_acceleration_rad_s2=float(item["max_acceleration_rad_s2"]),
            )
        except KeyError as exc:
            raise ValueError(
                f"joint mapping {item.get('joint_name', '<unknown>')} missing safety field {exc.args[0]}"
            ) from exc
        numeric = np.asarray(
            [
                limit.position_min_rad,
                limit.position_max_rad,
                limit.max_velocity_rad_s,
                limit.max_acceleration_rad_s2,
            ]
        )
        if not np.all(np.isfinite(numeric)):
            raise ValueError(f"{limit.joint_name}: safety limits must be finite")
        if limit.position_min_rad >= limit.position_max_rad:
            raise ValueError(f"{limit.joint_name}: position_min_rad must be less than maximum")
        if limit.max_velocity_rad_s <= 0 or limit.max_acceleration_rad_s2 <= 0:
            raise ValueError(f"{limit.joint_name}: velocity/acceleration limits must be positive")
        limits.append(limit)
    return limits


def validate_motion_safety(
    motor_positions: np.ndarray,
    timestamps: np.ndarray,
    config: dict[str, Any],
    *,
    label: str = "motion",
) -> dict[str, dict[str, float]]:
    """Apply the same joint-space limits to recorded and generated motion."""
    mappings = load_joint_mappings(config)
    limits = load_safety_limits(config)
    values = np.asarray(motor_positions, dtype=np.float64)
    times = np.asarray(timestamps, dtype=np.float64)
    if values.ndim != 2 or values.shape[0] == 0 or values.shape[1] != len(mappings):
        raise ValueError(f"{label}: motor positions must match the mapped joints")
    if times.shape != (values.shape[0],):
        raise ValueError(f"{label}: timestamps must match the motion rows")
    if not np.all(np.isfinite(values)) or not np.all(np.isfinite(times)):
        raise ValueError(f"{label}: positions and timestamps must be finite")
    dt = np.diff(times)
    if not np.all(np.isfinite(dt)) or np.any(dt <= 0):
        raise ValueError(f"{label}: time intervals must be finite and positive")
    joint_positions = motor_frames_to_joint(values, config)
    if not np.all(np.isfinite(joint_positions)):
        raise ValueError(f"{label}: converted joint positions must be finite")
    velocity = np.diff(joint_positions, axis=0) / dt[:, None]
    velocity_dt = (dt[:-1] + dt[1:]) * 0.5
    acceleration = np.diff(velocity, axis=0) / velocity_dt[:, None]
    max_velocity = {}
    max_acceleration = {}
    for index, limit in enumerate(limits):
        positions = joint_positions[:, index]
        if np.any(positions < limit.position_min_rad) or np.any(positions > limit.position_max_rad):
            raise ValueError(f"{label}: {limit.joint_name}: position limit exceeded")
        joint_velocity = float(np.max(np.abs(velocity[:, index]))) if velocity.size else 0.0
        joint_acceleration = (
            float(np.max(np.abs(acceleration[:, index]))) if acceleration.size else 0.0
        )
        if joint_velocity > limit.max_velocity_rad_s + 1e-9:
            raise ValueError(
                f"{label}: {limit.joint_name}: velocity {joint_velocity:.6g} exceeds "
                f"{limit.max_velocity_rad_s:.6g} rad/s"
            )
        if joint_acceleration > limit.max_acceleration_rad_s2 + 1e-9:
            raise ValueError(
                f"{label}: {limit.joint_name}: acceleration {joint_acceleration:.6g} exceeds "
                f"{limit.max_acceleration_rad_s2:.6g} rad/s^2"
            )
        max_velocity[limit.joint_name] = joint_velocity
        max_acceleration[limit.joint_name] = joint_acceleration
    return {
        "max_velocity_rad_s": max_velocity,
        "max_acceleration_rad_s2": max_acceleration,
    }


def validate_default_pose(config: dict[str, Any]) -> np.ndarray:
    mappings = load_joint_mappings(config)
    default_pose = np.asarray(config.get("default_pose_rad"), dtype=np.float64)
    if default_pose.shape != (len(mappings),):
        raise ValueError("default_pose_rad must contain one position per mapped motor")
    validate_motion_safety(default_pose[None, :], np.asarray([0.0]), config, label="default pose")
    return default_pose


def validate_hardware_config(config: dict[str, Any]) -> None:
    """Check execution settings before importing or initializing any device."""
    validate_default_pose(config)
    hardware = config.get("hardware", {})
    if not isinstance(hardware, dict):
        raise ValueError("hardware must be a mapping")
    for name, default, positive in (
        ("control_rate_hz", 200.0, True),
        ("transition_seconds", 2.0, True),
        ("kp", 20.0, False),
        ("kd", 0.5, False),
    ):
        value = float(hardware.get(name, default))
        if not np.isfinite(value) or (value <= 0 if positive else value < 0):
            requirement = "positive" if positive else "non-negative"
            raise ValueError(f"hardware.{name} must be finite and {requirement}")
    for item in hardware.get("motors", []):
        for name in ("kp", "kd", "sign", "zero_offset"):
            if name not in item:
                continue
            value = float(item[name])
            if not np.isfinite(value):
                raise ValueError(f"hardware motor {name} must be finite")
            if name in {"kp", "kd"} and value < 0:
                raise ValueError(f"hardware motor {name} must be non-negative")
            if name == "sign" and value == 0:
                raise ValueError("hardware motor sign must be non-zero")


def validate_trajectory_safety(
    trajectory: Trajectory,
    config: dict[str, Any],
    *,
    model_path: Path | None,
    require_model_fingerprint: bool,
    timing_rtol: float = 1e-3,
) -> dict[str, Any]:
    mappings = load_joint_mappings(config)
    expected_names = tuple(item.joint_name for item in mappings)
    if trajectory.limb != str(config.get("limb", "")):
        raise ValueError(
            f"trajectory limb {trajectory.limb!r} does not match config limb {config.get('limb')!r}"
        )
    if trajectory.joint_names != expected_names:
        raise ValueError("trajectory joint_names do not match configured motor mapping order")
    expected_config = fingerprint_config(config)
    if trajectory.config_fingerprint != expected_config:
        raise ValueError("trajectory config_fingerprint does not match current config")

    model_verified = False
    if model_path is not None and model_path.is_file():
        actual_model = fingerprint_file(model_path)
        if trajectory.model_fingerprint != actual_model:
            raise ValueError("trajectory model_fingerprint does not match configured model")
        model_verified = True
    elif require_model_fingerprint:
        raise ValueError("configured rp1_description model is required to verify model_fingerprint")
    elif trajectory.model_fingerprint != "not-provided":
        raise ValueError(
            "trajectory contains a model_fingerprint but no model was supplied for verification"
        )

    if "default_pose_rad" in config:
        validate_default_pose(config)
    if trajectory.time.size > 1:
        dt = np.diff(trajectory.time)
        expected_dt = 1.0 / trajectory.fps
        if not np.allclose(dt, expected_dt, rtol=timing_rtol, atol=1e-9):
            raise ValueError("trajectory time intervals are inconsistent with fps")
    motion = validate_motion_safety(
        trajectory.motor_pos, trajectory.time, config, label="trajectory"
    )

    return {
        "limb": trajectory.limb,
        "frames": int(trajectory.motor_pos.shape[0]),
        "fps": trajectory.fps,
        "config_fingerprint_verified": True,
        "model_fingerprint_verified": model_verified,
        "limits_verified": True,
        "timing_verified": True,
        **motion,
    }
