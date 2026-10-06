"""Narrow import bridge to the repository's existing motor helpers."""

from __future__ import annotations

# These imports intentionally reuse the proven CLI control primitives.
from scripts.log_motor_telemetry import ControllerHeartbeat, MotorTelemetryRecorder
from scripts.motor_feedback_watchdog import MotorFeedbackWatchdog
from scripts.rp1_limb_common import (
    MotorEntry,
    add_motors_python_paths,
    canonical_limb,
    command_mit,
    compute_hip_roll_grav_torque,
    create_motor_driver,
    hip_roll_grav_comp_enabled,
    limb_joint_to_motor,
    limb_motor_entries,
    limb_motor_to_joint,
    load_motor_position_npz,
    load_yaml_config,
    motor_default_positions,
    trajectory_target_at,
)


__all__ = [
    "ControllerHeartbeat",
    "MotorEntry",
    "MotorFeedbackWatchdog",
    "MotorTelemetryRecorder",
    "add_motors_python_paths",
    "canonical_limb",
    "command_mit",
    "compute_hip_roll_grav_torque",
    "create_motor_driver",
    "hip_roll_grav_comp_enabled",
    "limb_joint_to_motor",
    "limb_motor_entries",
    "limb_motor_to_joint",
    "load_motor_position_npz",
    "load_yaml_config",
    "motor_default_positions",
    "trajectory_target_at",
]
