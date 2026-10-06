"""Configuration loading for supported factory-aging stations."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from importlib import resources
import os
from pathlib import Path
from typing import Any, Mapping, Sequence

import yaml

from factory_hmi.models import SafetyLimits, StationConfig

from ._legacy import (
    canonical_limb,
    limb_motor_entries,
    load_yaml_config,
)


SUPPORTED_LIMBS = frozenset(
    {
        "left_arm",
        "right_arm",
        "left_short_arm",
        "right_short_arm",
        "left_leg",
        "right_leg",
        "left_short_leg",
        "right_short_leg",
        "waist_hip",
        "biped_waist",
        "upper_body",
    }
)

CONFIG_FILENAMES: dict[str, str] = {
    "left_arm": "left_arm_motors.yaml",
    "right_arm": "right_arm_motors.yaml",
    "left_short_arm": "left_short_arm_motors.yaml",
    "right_short_arm": "right_short_arm_motors.yaml",
    "left_leg": "left_leg_motors.yaml",
    "right_leg": "right_leg_motors.yaml",
    "left_short_leg": "left_short_leg_motors.yaml",
    "right_short_leg": "right_short_leg_motors.yaml",
    "waist_hip": "waist_hip_motors.yaml",
    "biped_waist": "biped_waist_motors.yaml",
    "upper_body": "upper_body_motors.yaml",
}
SYSTEM_CONFIG_ROOT = Path(
    os.environ.get(
        "RP1_FACTORY_ALLOWED_CONFIG_ROOT",
        "/etc/rp1-factory-hmi/stations",
    )
).expanduser()
DEFAULT_CONFIG_PATHS: dict[str, Path] = {
    limb: SYSTEM_CONFIG_ROOT / filename
    for limb, filename in CONFIG_FILENAMES.items()
}
DEFAULT_HMI_CONFIG_PATH = Path(
    os.environ.get(
        "RP1_FACTORY_DEFAULTS_PATH",
        "/etc/rp1-factory-hmi/station_defaults.yaml",
    )
).expanduser()


def _resource_yaml(package: str, *parts: str) -> dict[str, Any]:
    resource = resources.files(package).joinpath(*parts)
    data = yaml.safe_load(resource.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"embedded resource {resource.name} must contain a YAML mapping")
    return data


def _load_station_yaml(canonical: str, path: Path | str | None) -> dict[str, Any]:
    if path is not None:
        return load_yaml_config(path)
    external = DEFAULT_CONFIG_PATHS[canonical]
    try:
        if external.is_file():
            return load_yaml_config(external)
    except OSError:
        pass
    return _resource_yaml("scripts", "config", CONFIG_FILENAMES[canonical])


def _load_hmi_defaults() -> dict[str, Any]:
    try:
        if DEFAULT_HMI_CONFIG_PATH.is_file():
            return load_yaml_config(DEFAULT_HMI_CONFIG_PATH)
    except OSError:
        pass
    return _resource_yaml("factory_hmi", "config", "station_defaults.yaml")


def _merge_missing(target: dict[str, Any], defaults: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(target)
    for key, value in defaults.items():
        if key not in result:
            result[key] = deepcopy(value)
        elif isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _merge_missing(result[key], value)
    return result


def require_supported_limb(limb: str) -> str:
    canonical = canonical_limb(limb)
    if canonical not in SUPPORTED_LIMBS:
        valid = ", ".join(sorted(SUPPORTED_LIMBS))
        raise ValueError(f"factory HMI does not support {canonical!r}; valid limbs: {valid}")
    return canonical


def _configured_control_rate(config: dict[str, Any]) -> float:
    hmi = config.get("factory_hmi", {})
    if hmi is None:
        hmi = {}
    if not isinstance(hmi, dict):
        raise ValueError("factory_hmi must be a mapping")
    return float(hmi.get("control_rate_hz", config.get("control_rate_hz", 200.0)))


def _motor_bus_counts(config: Mapping[str, Any]) -> tuple[int, ...]:
    raw = config.get("motor_num")
    if not isinstance(raw, list) or not raw:
        raise ValueError("motor_num must be a non-empty list")
    counts: list[int] = []
    for value in raw:
        if isinstance(value, bool):
            raise ValueError("motor_num must contain positive integers")
        count = int(value)
        if count <= 0:
            raise ValueError("motor_num must contain positive integers")
        counts.append(count)
    return tuple(counts)


def _default_bus_interfaces(config: Mapping[str, Any]) -> tuple[str, ...]:
    """Preserve bus-sharing topology while renumbering defaults from can0."""

    counts = _motor_bus_counts(config)
    raw = config.get("motor_interface")
    if not isinstance(raw, list) or len(raw) != len(counts):
        return tuple(f"can{index}" for index in range(len(counts)))
    normalized: dict[str, str] = {}
    result: list[str] = []
    for value in raw:
        source_name = str(value).strip()
        if not source_name:
            source_name = f"__group_{len(result)}"
        if source_name not in normalized:
            normalized[source_name] = f"can{len(normalized)}"
        result.append(normalized[source_name])
    return tuple(result)


def _config_with_bus_bindings(
    config: dict[str, Any],
    bus_bindings: Sequence[Mapping[str, Any]] | None,
) -> dict[str, Any]:
    """Apply HMI-selected buses without consuming CAN fields from the YAML."""

    if bus_bindings is None:
        return config
    counts = _motor_bus_counts(config)
    if len(bus_bindings) != len(counts):
        raise ValueError(
            f"bus_bindings must contain {len(counts)} items, got {len(bus_bindings)}"
        )
    interfaces: list[str] = []
    interface_types: list[str] = []
    for expected_index, raw in enumerate(bus_bindings):
        if not isinstance(raw, Mapping):
            raise ValueError("each bus binding must be a mapping")
        index = raw.get("index", expected_index)
        if isinstance(index, bool) or int(index) != expected_index:
            raise ValueError("bus binding indices must be contiguous and ordered")
        interface = str(raw.get("interface", "")).strip()
        if not interface:
            raise ValueError(f"bus binding {expected_index} has no interface")
        mode = str(raw.get("mode", raw.get("interface_type", ""))).strip().lower()
        if mode not in {"can", "canfd"}:
            raise ValueError("bus binding mode must be can or canfd")
        interfaces.append(interface)
        interface_types.append(mode)
    result = deepcopy(config)
    result["motor_interface"] = interfaces
    result["motor_interface_type"] = interface_types
    return result


_MOTOR_OVERRIDE_FIELDS = {
    "motor_id": int,
    "motor_type": str,
    "motor_model": int,
    "zero_offset": float,
    "kp": float,
    "kd": float,
    "sign": float,
}


def _apply_motor_overrides(
    entries: tuple[Any, ...],
    motor_overrides: Sequence[Mapping[str, Any]] | None,
) -> tuple[Any, ...]:
    if motor_overrides is None:
        return entries
    result = list(entries)
    seen: set[int] = set()
    for raw in motor_overrides:
        if not isinstance(raw, Mapping):
            raise ValueError("each motor override must be a mapping")
        index = raw.get("index")
        if isinstance(index, bool) or not isinstance(index, int):
            raise ValueError("motor override index must be an integer")
        if index < 0 or index >= len(result):
            raise ValueError(f"motor override index {index} is out of range")
        if index in seen:
            raise ValueError(f"duplicate motor override index {index}")
        seen.add(index)
        changes: dict[str, Any] = {}
        for name, cast in _MOTOR_OVERRIDE_FIELDS.items():
            if name not in raw or raw[name] is None:
                continue
            value = cast(raw[name])
            if name == "motor_type":
                value = str(value).strip().upper()
                if not value:
                    raise ValueError("motor_type cannot be empty")
            changes[name] = value
        result[index] = replace(result[index], **changes)
    return tuple(result)


def build_station_config(
    config: dict[str, Any],
    limb: str,
    *,
    control_rate_hz: float | None = None,
    safety_limits: SafetyLimits | None = None,
    bus_bindings: Sequence[Mapping[str, Any]] | None = None,
    motor_overrides: Sequence[Mapping[str, Any]] | None = None,
) -> StationConfig:
    canonical = require_supported_limb(limb)
    bound_config = _config_with_bus_bindings(config, bus_bindings)
    entries = _apply_motor_overrides(
        tuple(limb_motor_entries(bound_config, canonical)),
        motor_overrides,
    )
    return StationConfig(
        limb=canonical,
        entries=entries,
        raw=config,
        control_rate_hz=(
            _configured_control_rate(config)
            if control_rate_hz is None
            else float(control_rate_hz)
        ),
        safety_limits=safety_limits or SafetyLimits.from_config(config),
    )


def load_station_config(
    limb: str,
    path: Path | str | None = None,
    *,
    control_rate_hz: float | None = None,
    safety_limits: SafetyLimits | None = None,
    bus_bindings: Sequence[Mapping[str, Any]] | None = None,
    motor_overrides: Sequence[Mapping[str, Any]] | None = None,
) -> StationConfig:
    canonical = require_supported_limb(limb)
    config = _load_station_yaml(canonical, path)
    config = _merge_missing(config, _load_hmi_defaults())
    return build_station_config(
        config,
        canonical,
        control_rate_hz=control_rate_hz,
        safety_limits=safety_limits,
        bus_bindings=bus_bindings,
        motor_overrides=motor_overrides,
    )


def preview_station_config(
    limb: str,
    path: Path | str,
) -> dict[str, Any]:
    """Return editable motor data while deliberately discarding YAML CAN data."""

    canonical = require_supported_limb(limb)
    config = _load_station_yaml(canonical, path)
    config = _merge_missing(config, _load_hmi_defaults())
    counts = _motor_bus_counts(config)
    motor_count = sum(counts)
    if canonical in {"left_arm", "left_short_arm"} and motor_count == 5:
        canonical = "left_short_arm"
    elif canonical in {"right_arm", "right_short_arm"} and motor_count == 5:
        canonical = "right_short_arm"
    elif canonical == "left_short_arm" and motor_count == 7:
        canonical = "left_arm"
    elif canonical == "right_short_arm" and motor_count == 7:
        canonical = "right_arm"
    default_interfaces = _default_bus_interfaces(config)
    placeholders = [
        {"index": index, "interface": interface, "mode": "canfd"}
        for index, interface in enumerate(default_interfaces)
    ]
    station = build_station_config(config, canonical, bus_bindings=placeholders)
    motors = [
        {
            "index": int(entry.index),
            "joint_name": entry.joint_name,
            "motor_id": int(entry.motor_id),
            "motor_type": entry.motor_type,
            "motor_model": int(entry.motor_model),
            "zero_offset": float(entry.zero_offset),
            "kp": float(entry.kp),
            "kd": float(entry.kd),
            "sign": float(entry.sign),
        }
        for entry in station.entries
    ]
    return {
        "limb": canonical,
        "bus_groups": [
            {
                "index": index,
                "motor_count": count,
                "default_interface": default_interfaces[index],
            }
            for index, count in enumerate(counts)
        ],
        "motors": motors,
    }


__all__ = [
    "DEFAULT_CONFIG_PATHS",
    "DEFAULT_HMI_CONFIG_PATH",
    "CONFIG_FILENAMES",
    "SYSTEM_CONFIG_ROOT",
    "SUPPORTED_LIMBS",
    "build_station_config",
    "load_station_config",
    "preview_station_config",
    "require_supported_limb",
]
