"""Explicit platform mapping and telemetry capability declarations."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

TELEMETRY_SUBJECT_PATTERN = re.compile(r"^[A-Z0-9]+(?:-[A-Z0-9]+)*$")
ALLOWED_TARGET_PART_CODES = {
    "SARM",
    "SLEG",
    "SYS",
    "UPPER",
    "LOWER",
    "CHEST",
    "HEAD",
    "BAT",
}


@dataclass(frozen=True)
class JointMapping:
    joint_name: str
    motor_index: int
    telemetry_subject_code: str
    target_part_code: str


def load_joint_mappings(config: dict[str, Any]) -> list[JointMapping]:
    raw = config.get("joint_mappings")
    if not isinstance(raw, list) or not raw:
        raise ValueError("config joint_mappings must be a non-empty list")
    mappings = [
        JointMapping(
            joint_name=str(item["joint_name"]),
            motor_index=int(item["motor_index"]),
            telemetry_subject_code=str(item["telemetry_subject_code"]),
            target_part_code=str(item["target_part_code"]),
        )
        for item in raw
    ]
    names = [item.joint_name for item in mappings]
    indexes = [item.motor_index for item in mappings]
    subjects = [item.telemetry_subject_code for item in mappings]
    if len(set(names)) != len(names):
        raise ValueError("duplicate joint_name in joint_mappings")
    if sorted(indexes) != list(range(len(indexes))):
        raise ValueError("motor_index must be unique and contiguous from zero")
    if len(set(subjects)) != len(subjects):
        raise ValueError("duplicate telemetry_subject_code in joint_mappings")
    for item in mappings:
        if not TELEMETRY_SUBJECT_PATTERN.fullmatch(item.telemetry_subject_code):
            raise ValueError(
                f"invalid telemetry_subject_code {item.telemetry_subject_code!r}; "
                "expected ^[A-Z0-9]+(-[A-Z0-9]+)*$"
            )
        if item.target_part_code not in ALLOWED_TARGET_PART_CODES:
            allowed = ", ".join(sorted(ALLOWED_TARGET_PART_CODES))
            raise ValueError(
                f"invalid target_part_code {item.target_part_code!r}; allowed: {allowed}"
            )
    return sorted(mappings, key=lambda item: item.motor_index)


def capability_gaps(config: dict[str, Any]) -> dict[str, str]:
    gaps = config.get("telemetry_gaps", {})
    required = ("bus_voltage", "vibration")
    if not isinstance(gaps, dict) or any(not str(gaps.get(key, "")).strip() for key in required):
        raise ValueError("telemetry_gaps must explicitly describe bus_voltage and vibration")
    return {key: str(gaps[key]) for key in required}


def error_event(mapping: JointMapping, error_id: int, *, elapsed_s: float) -> dict[str, Any]:
    if int(error_id) == 0:
        raise ValueError("error events require a non-zero error_id")
    return {
        "type": "motor_error",
        "elapsed_s": float(elapsed_s),
        "joint_name": mapping.joint_name,
        "telemetry_subject_code": mapping.telemetry_subject_code,
        "target_part_code": mapping.target_part_code,
        "error_id": int(error_id),
    }
