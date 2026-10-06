"""Data models shared by the factory-aging control core.

The models in this module deliberately contain no Qt/Tk/web dependencies.  A
desktop or web HMI can therefore consume controller snapshots without being
part of the real-time control path.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Sequence

import numpy as np


class StationState(str, Enum):
    DISCONNECTED = "disconnected"
    CONNECTED = "connected"
    ARMED = "armed"
    RUNNING = "running"
    PAUSED = "paused"
    STOPPING = "stopping"
    COMPLETED = "completed"
    FAULT = "fault"


class ZeroingState(str, Enum):
    IDLE = "idle"
    PREPARED = "prepared"
    READING = "reading"
    COMPLETED = "completed"
    ABORTED = "aborted"


def json_number(value: float | int | None) -> float | int | None:
    """Return a strict-JSON number, replacing NaN/inf with ``None``."""

    if value is None or isinstance(value, int):
        return value
    number = float(value)
    return number if math.isfinite(number) else None


@dataclass(frozen=True)
class MotorSample:
    """One cached motor-feedback sample.

    Field names match ``scripts/log_motor_telemetry.py`` where practical so
    existing recorder and diagnosis code can exchange samples cheaply.
    """

    pos_rad: float
    cmd_pos_rad: float = float("nan")
    spd_rad_s: float = 0.0
    torque_nm: float = 0.0
    temp_c: float = float("nan")
    error_id: int = 0
    bus_voltage_v: float = float("nan")
    bus_current_a: float = float("nan")
    motor_id: int = 0
    index: int = 0
    joint_name: str = ""
    bus: str = ""
    timestamp_s: float = 0.0
    feedback_age_s: float = 0.0

    @property
    def position(self) -> float:
        return self.pos_rad

    @property
    def velocity(self) -> float:
        return self.spd_rad_s

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": int(self.index),
            "motor_id": int(self.motor_id),
            "joint_name": self.joint_name,
            "bus": self.bus,
            "position_rad": json_number(self.pos_rad),
            "command_position_rad": json_number(self.cmd_pos_rad),
            "speed_rad_s": json_number(self.spd_rad_s),
            "torque_nm": json_number(self.torque_nm),
            "temperature_c": json_number(self.temp_c),
            "error_id": int(self.error_id),
            "bus_voltage_v": json_number(self.bus_voltage_v),
            "bus_current_a": json_number(self.bus_current_a),
            "timestamp_s": json_number(self.timestamp_s),
            "feedback_age_s": json_number(self.feedback_age_s),
        }


@dataclass(frozen=True)
class SafetyLimits:
    """Configurable trajectory and initial-pose safety bounds."""

    motor_min_rad: float | Sequence[float] | None = None
    motor_max_rad: float | Sequence[float] | None = None
    joint_min_rad: float | Sequence[float] | None = None
    joint_max_rad: float | Sequence[float] | None = None
    max_frame_delta_rad: float | Sequence[float] | None = None
    max_velocity_rad_s: float | Sequence[float] | None = None
    max_first_frame_delta_rad: float | Sequence[float] | None = None

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> "SafetyLimits":
        hmi = config.get("factory_hmi", {})
        if hmi is None:
            hmi = {}
        if not isinstance(hmi, dict):
            raise ValueError("factory_hmi must be a mapping")
        raw = config.get(
            "factory_hmi_safety",
            config.get("safety", hmi.get("safety", config)),
        )
        if raw is None:
            raw = {}
        if not isinstance(raw, dict):
            raise ValueError("factory_hmi_safety/safety must be a mapping")

        aliases = {
            "motor_min_rad": ("motor_min_rad", "motor_position_min_rad"),
            "motor_max_rad": ("motor_max_rad", "motor_position_max_rad"),
            "joint_min_rad": ("joint_min_rad", "joint_position_min_rad"),
            "joint_max_rad": ("joint_max_rad", "joint_position_max_rad"),
            "max_frame_delta_rad": ("max_frame_delta_rad",),
            "max_velocity_rad_s": ("max_velocity_rad_s",),
            "max_first_frame_delta_rad": ("max_first_frame_delta_rad",),
        }
        values: dict[str, Any] = {}
        for field_name, keys in aliases.items():
            for key in keys:
                if key in raw:
                    values[field_name] = raw[key]
                    break
        return cls(**values)


@dataclass(frozen=True)
class PreflightReport:
    safe: bool
    motor_count: int
    frame_count: int
    max_frame_delta_rad: float
    max_velocity_rad_s: float
    max_first_frame_delta_rad: float | None
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "safe": bool(self.safe),
            "motor_count": int(self.motor_count),
            "frame_count": int(self.frame_count),
            "max_frame_delta_rad": json_number(self.max_frame_delta_rad),
            "max_velocity_rad_s": json_number(self.max_velocity_rad_s),
            "max_first_frame_delta_rad": json_number(self.max_first_frame_delta_rad),
            "warnings": list(self.warnings),
        }


@dataclass
class TrajectoryData:
    path: Path
    limb: str
    motor_pos: np.ndarray
    timestamps: np.ndarray
    fps: float
    sha256: str
    preview: dict[str, Any]
    preflight: PreflightReport | None = None

    @property
    def frame_count(self) -> int:
        return int(self.motor_pos.shape[0])

    @property
    def motor_count(self) -> int:
        return int(self.motor_pos.shape[1])

    @property
    def duration_s(self) -> float:
        return float(self.timestamps[-1]) if self.timestamps.size else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "limb": self.limb,
            "sha256": self.sha256,
            "fps": float(self.fps),
            "frames": self.frame_count,
            "motors": self.motor_count,
            "duration_s": self.duration_s,
            "preview": self.preview,
            "preflight": self.preflight.to_dict() if self.preflight else None,
        }


@dataclass(frozen=True)
class PlaybackOptions:
    loop: bool = False
    cycles: int = 0
    duration_s: float | None = None
    speed: float = 1.0

    def __post_init__(self) -> None:
        if self.cycles < 0:
            raise ValueError("cycles must be >= 0")
        if not math.isfinite(float(self.speed)) or self.speed <= 0.0:
            raise ValueError("speed must be a positive finite number")
        if self.duration_s is not None and (
            not math.isfinite(float(self.duration_s)) or self.duration_s <= 0.0
        ):
            raise ValueError("duration_s must be a positive finite number")


@dataclass
class PlaybackProgress:
    cycle: int = 0
    completed_cycles: int = 0
    frame: int = 0
    active_seconds: float = 0.0
    trajectory_seconds: float = 0.0
    fraction: float = 0.0
    command_count: int = 0
    outcome: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "cycle": int(self.cycle),
            "completed_cycles": int(self.completed_cycles),
            "frame": int(self.frame),
            "active_seconds": json_number(self.active_seconds),
            "trajectory_seconds": json_number(self.trajectory_seconds),
            "fraction": json_number(self.fraction),
            "command_count": int(self.command_count),
            "outcome": self.outcome,
        }


@dataclass(frozen=True)
class FaultDiagnosis:
    suspected_component: str
    evidence: tuple[str, ...]
    action: str
    severity: str
    confidence: float
    rule: str
    affected_motor_ids: tuple[int, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "suspected_component": self.suspected_component,
            "evidence": list(self.evidence),
            "action": self.action,
            "severity": self.severity,
            "confidence": float(max(0.0, min(1.0, self.confidence))),
            "rule": self.rule,
            "affected_motor_ids": [int(item) for item in self.affected_motor_ids],
        }


@dataclass
class FaultStatus:
    message: str
    kind: str = "controller"
    timestamp_s: float = 0.0
    diagnoses: list[FaultDiagnosis] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "message": self.message,
            "kind": self.kind,
            "timestamp_s": json_number(self.timestamp_s),
            "diagnoses": [item.to_dict() for item in self.diagnoses],
        }


@dataclass(frozen=True)
class StationConfig:
    limb: str
    entries: tuple[Any, ...]
    raw: dict[str, Any]
    control_rate_hz: float = 200.0
    safety_limits: SafetyLimits = field(default_factory=SafetyLimits)

    def __post_init__(self) -> None:
        if not math.isfinite(float(self.control_rate_hz)) or self.control_rate_hz <= 0.0:
            raise ValueError("control_rate_hz must be a positive finite number")
