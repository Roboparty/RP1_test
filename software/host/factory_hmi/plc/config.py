"""PLC cabinet configuration loader with strict, address-free domain settings."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

DEFAULT_CONFIG_PATH = (
    Path(__file__).resolve().parents[1] / "config" / "plc_cabinet.yaml"
)


@dataclass(frozen=True)
class PlcTimingConfig:
    heartbeat_ms: int = 500
    poll_ms: int = 200
    stale_timeout_ms: int = 1000
    command_timeout_ms: int = 5000
    km0_timeout_ms: int = 5000
    ps1_timeout_ms: int = 5000
    reconnect_ms: int = 2000
    trend_retention_seconds: int = 300


@dataclass(frozen=True)
class PlcSetpointConfig:
    enabled: bool = False
    voltage_min_v: float = 0.0
    voltage_max_v: float = 0.0
    current_min_a: float = 0.0
    current_max_a: float = 0.0
    step: float = 0.001


@dataclass(frozen=True)
class PlcCabinetConfig:
    driver: str = "mock"
    access_mode: str = "control"
    timings: PlcTimingConfig = field(default_factory=PlcTimingConfig)
    setpoints: PlcSetpointConfig = field(default_factory=PlcSetpointConfig)
    protocol: dict[str, Any] = field(default_factory=dict)
    dut_can_map: dict[str, str] = field(
        default_factory=lambda: {
            "DUT1": "can0",
            "DUT2": "can1",
            "DUT3": "can2",
            "DUT4": "can3",
        }
    )


def _positive_int(source: Mapping[str, Any], key: str, default: int) -> int:
    value = int(source.get(key, default))
    if value <= 0:
        raise ValueError(f"plc timings.{key} must be positive")
    return value


def _finite_float(source: Mapping[str, Any], key: str, default: float) -> float:
    value = float(source.get(key, default))
    if not math.isfinite(value):
        raise ValueError(f"plc setpoints.{key} must be finite")
    return value


def load_plc_config(path: Path | str | None = None) -> PlcCabinetConfig:
    config_path = Path(path or DEFAULT_CONFIG_PATH).expanduser().resolve()
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, Mapping):
        raise ValueError("PLC configuration root must be a mapping")
    driver = str(raw.get("driver", "mock")).strip().lower()
    if driver not in {"mock", "s7", "modbus_tcp"}:
        raise ValueError("PLC driver must be mock, s7, or modbus_tcp")
    access_mode = str(raw.get("access_mode", "control")).strip().lower()
    if access_mode not in {"monitor", "control"}:
        raise ValueError("PLC access_mode must be monitor or control")
    timing_raw = raw.get("timings") or {}
    if not isinstance(timing_raw, Mapping):
        raise ValueError("plc timings must be a mapping")
    defaults = PlcTimingConfig()
    timings = PlcTimingConfig(
        heartbeat_ms=_positive_int(timing_raw, "heartbeat_ms", defaults.heartbeat_ms),
        poll_ms=_positive_int(timing_raw, "poll_ms", defaults.poll_ms),
        stale_timeout_ms=_positive_int(
            timing_raw, "stale_timeout_ms", defaults.stale_timeout_ms
        ),
        command_timeout_ms=_positive_int(
            timing_raw, "command_timeout_ms", defaults.command_timeout_ms
        ),
        km0_timeout_ms=_positive_int(
            timing_raw, "km0_timeout_ms", defaults.km0_timeout_ms
        ),
        ps1_timeout_ms=_positive_int(
            timing_raw, "ps1_timeout_ms", defaults.ps1_timeout_ms
        ),
        reconnect_ms=_positive_int(timing_raw, "reconnect_ms", defaults.reconnect_ms),
        trend_retention_seconds=_positive_int(
            timing_raw,
            "trend_retention_seconds",
            defaults.trend_retention_seconds,
        ),
    )
    if not 100 <= timings.poll_ms <= 250:
        raise ValueError("plc timings.poll_ms must be in [100, 250]")
    if timings.stale_timeout_ms <= timings.poll_ms:
        raise ValueError("stale_timeout_ms must exceed poll_ms")
    setpoint_raw = raw.get("setpoints") or {}
    if not isinstance(setpoint_raw, Mapping):
        raise ValueError("plc setpoints must be a mapping")
    setpoints = PlcSetpointConfig(
        enabled=bool(setpoint_raw.get("enabled", False)),
        voltage_min_v=_finite_float(setpoint_raw, "voltage_min_v", 0.0),
        voltage_max_v=_finite_float(setpoint_raw, "voltage_max_v", 0.0),
        current_min_a=_finite_float(setpoint_raw, "current_min_a", 0.0),
        current_max_a=_finite_float(setpoint_raw, "current_max_a", 0.0),
        step=_finite_float(setpoint_raw, "step", 0.001),
    )
    if setpoints.enabled:
        if not 0.0 < setpoints.voltage_min_v < setpoints.voltage_max_v:
            raise ValueError("invalid PLC voltage setpoint range")
        if not 0.0 < setpoints.current_min_a < setpoints.current_max_a:
            raise ValueError("invalid PLC current setpoint range")
        if setpoints.step <= 0.0:
            raise ValueError("plc setpoints.step must be positive")
    can_map = raw.get("dut_can_map") or {}
    expected = {f"DUT{index}": f"can{index - 1}" for index in range(1, 5)}
    if dict(can_map) != expected:
        raise ValueError(f"dut_can_map is fixed and must be {expected}")
    protocol = raw.get(driver) or {}
    if not isinstance(protocol, Mapping):
        raise ValueError(f"plc {driver} configuration must be a mapping")
    return PlcCabinetConfig(
        driver=driver,
        access_mode=access_mode,
        timings=timings,
        setpoints=setpoints,
        protocol=dict(protocol),
        dut_can_map=expected,
    )


__all__ = [
    "DEFAULT_CONFIG_PATH",
    "PlcCabinetConfig",
    "PlcSetpointConfig",
    "PlcTimingConfig",
    "load_plc_config",
]
