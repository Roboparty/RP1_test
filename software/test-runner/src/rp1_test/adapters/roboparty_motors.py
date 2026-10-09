"""Adapter for the RoboParty ``motors_py`` SDK.

The SDK is discovered from the normal Python environment first, then from
``RP1_MOTORS_SDK_ROOT``. No repository-specific absolute path is embedded.
"""

from __future__ import annotations

import importlib
import math
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rp1_test.mapping import load_joint_mappings


def _add_sdk_paths(root: Path) -> None:
    candidates = [
        root / "build",
        *root.glob("install/*/lib/python*/site-packages"),
        *root.glob("install/lib/python*/site-packages"),
        *root.glob("lib/python*/site-packages"),
        *root.glob("build/*"),
    ]
    for candidate in reversed(candidates):
        path = str(candidate.resolve())
        if candidate.is_dir() and path not in sys.path:
            sys.path.insert(0, path)


def import_motors_py() -> Any:
    try:
        return importlib.import_module("motors_py")
    except ImportError:
        root_value = os.environ.get("RP1_MOTORS_SDK_ROOT", "").strip()
        if not root_value:
            raise RuntimeError(
                "motors_py is not installed and RP1_MOTORS_SDK_ROOT is unset; "
                "see docs/dependencies.md"
            ) from None
        root = Path(root_value).expanduser().resolve()
        if not root.is_dir():
            raise RuntimeError(f"RP1_MOTORS_SDK_ROOT is not a directory: {root}")
        _add_sdk_paths(root)
        try:
            return importlib.import_module("motors_py")
        except ImportError as exc:
            raise RuntimeError(
                f"motors_py was not found under RP1_MOTORS_SDK_ROOT={root}; "
                "build/install the approved SDK release first"
            ) from exc


@dataclass(frozen=True)
class MotorConfig:
    motor_index: int
    interface: str
    interface_type: str
    backend: str
    motor_type: str
    motor_model: int
    motor_id: int
    master_id_offset: int
    zero_offset: float
    sign: float
    kp: float
    kd: float


def load_motor_configs(config: dict[str, Any]) -> list[MotorConfig]:
    hardware = config.get("hardware")
    raw = hardware.get("motors") if isinstance(hardware, dict) else None
    if not isinstance(raw, list) or not raw:
        raise ValueError("hardware.motors must be a non-empty list")
    result = [
        MotorConfig(
            motor_index=int(item["motor_index"]),
            interface=str(item["interface"]),
            interface_type=str(item["interface_type"]).lower(),
            backend=str(item.get("backend", "socketcan")).lower(),
            motor_type=str(item["motor_type"]).upper(),
            motor_model=int(item["motor_model"]),
            motor_id=int(item["motor_id"]),
            master_id_offset=int(item.get("master_id_offset", 0)),
            zero_offset=float(item["zero_offset"]),
            sign=float(item["sign"]),
            kp=float(item["kp"]),
            kd=float(item["kd"]),
        )
        for item in raw
    ]
    result.sort(key=lambda item: item.motor_index)
    if [item.motor_index for item in result] != list(range(len(result))):
        raise ValueError("hardware.motors motor_index must be contiguous from zero")
    if len(result) != len(load_joint_mappings(config)):
        raise ValueError("hardware.motors count must match joint_mappings")
    if any(
        not math.isfinite(value)
        for item in result
        for value in (item.sign, item.zero_offset, item.kp, item.kd)
    ):
        raise ValueError("hardware motor sign/zero_offset/kp/kd must be finite")
    if any(item.sign == 0.0 for item in result):
        raise ValueError("hardware motor sign must be non-zero")
    if any(item.kp < 0.0 or item.kd < 0.0 for item in result):
        raise ValueError("hardware motor kp/kd must be non-negative")
    return result


class RoboPartyMotor:
    def __init__(self, driver: Any, motor_config: MotorConfig) -> None:
        self.driver = driver
        self.config = motor_config
        self.capabilities = {
            "position": hasattr(driver, "get_motor_pos"),
            "speed": hasattr(driver, "get_motor_spd"),
            "torque_nm": hasattr(driver, "get_motor_current"),
            "temperature_c": hasattr(driver, "get_motor_temperature"),
            "error_id": hasattr(driver, "get_error_id"),
            "bus_voltage_v": hasattr(driver, "get_motor_dc_bus_voltage"),
            "vibration": False,
        }

    def command_mit(self, position_rad: float, *, kp: float, kd: float) -> None:
        del kp, kd
        self.driver.motor_mit_cmd(
            float(position_rad),
            0.0,
            self.config.kp,
            self.config.kd,
            0.0,
        )

    def get_position(self) -> float:
        return float(self.driver.get_motor_pos())

    def get_speed(self) -> float:
        return float(self.driver.get_motor_spd())

    def get_torque(self) -> float:
        return float(self.driver.get_motor_current())

    def get_temperature(self) -> float:
        return float(self.driver.get_motor_temperature())

    def get_error_id(self) -> int:
        return int(self.driver.get_error_id())

    def disable(self) -> None:
        self.driver.deinit_motor()


def _create_driver(motors_py: Any, item: MotorConfig) -> Any:
    kwargs = {
        "motor_id": item.motor_id,
        "interface_type": item.interface_type,
        "interface": item.interface,
        "motor_type": item.motor_type,
        "motor_model": item.motor_model,
        "master_id_offset": item.master_id_offset,
        "motor_zero_offset": item.zero_offset,
    }
    try:
        return motors_py.MotorDriver.create_motor(**kwargs, backend=item.backend)
    except TypeError as exc:
        if item.backend != "socketcan":
            raise RuntimeError(
                "installed motors_py does not support selectable CAN backend"
            ) from exc
        return motors_py.MotorDriver.create_motor(**kwargs)


def create_motors(config: dict[str, Any]) -> list[RoboPartyMotor]:
    motor_configs = load_motor_configs(config)
    motors_py = import_motors_py()
    motors: list[RoboPartyMotor] = []
    drivers = []
    try:
        for item in motor_configs:
            driver = _create_driver(motors_py, item)
            drivers.append(driver)  # Track before init/mode switching can fail.
            driver.init_motor()
            driver.set_motor_control_mode(motors_py.MotorControlMode.MIT)
            motors.append(RoboPartyMotor(driver, item))
    except BaseException as exc:
        for driver in reversed(drivers):
            try:
                driver.deinit_motor()
            except BaseException as cleanup_error:
                if hasattr(exc, "add_note"):
                    exc.add_note(f"Motor initialization rollback failed: {cleanup_error}")
        raise
    return motors
