#!/usr/bin/env python3
"""Shared helpers for RP1 single-limb motor and simulation tests."""

from __future__ import annotations

import glob
import math
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import yaml

try:
    from scripts.rp1_close_chain import ankle_motor_to_joint, leg_joint_to_motor
except ModuleNotFoundError:
    # Keep direct ``python scripts/<tool>.py`` execution compatible.
    from rp1_close_chain import ankle_motor_to_joint, leg_joint_to_motor


SCRIPT_ROOT = Path(__file__).resolve().parent
PACKAGE_ROOT = SCRIPT_ROOT.parent
MOTORS_ROOT = PACKAGE_ROOT / "motors"
DESCRIPTION_ROOT = PACKAGE_ROOT / "rp1_description"
CONFIG_ROOT = SCRIPT_ROOT / "config"
DEFAULT_CONFIG = CONFIG_ROOT / "limb_motors.yaml"
DEFAULT_ZERO_CONFIG = CONFIG_ROOT / "set_zero.yaml"
DEFAULT_SCENE_XML = DESCRIPTION_ROOT / "mjcf" / "rp1_flat.xml"
BATCH_MIT_INTERFACE_TYPES = {"canfd", "ethercanfd"}
BATCH_MIT_MOTOR_TYPES = {"EVO", "LRO", "XYN"}


LIMB_JOINT_NAMES = {
    "right_leg": [
        "right_hip_pitch_joint",
        "right_hip_roll_joint",
        "right_hip_yaw_joint",
        "right_knee_joint",
        "right_ankle_pitch_joint",
        "right_ankle_roll_joint",
    ],
    "left_leg": [
        "left_hip_pitch_joint",
        "left_hip_roll_joint",
        "left_hip_yaw_joint",
        "left_knee_joint",
        "left_ankle_pitch_joint",
        "left_ankle_roll_joint",
    ],
    # 5-DoF legs without hip_pitch (motor id 2 is reserved for waist_hip).
    "right_short_leg": [
        "right_hip_roll_joint",
        "right_hip_yaw_joint",
        "right_knee_joint",
        "right_ankle_pitch_joint",
        "right_ankle_roll_joint",
    ],
    "left_short_leg": [
        "left_hip_roll_joint",
        "left_hip_yaw_joint",
        "left_knee_joint",
        "left_ankle_pitch_joint",
        "left_ankle_roll_joint",
    ],
    # Waist (id 1 x2) + left/right hip_pitch (id 2 x2).
    "waist_hip": [
        "waist_roll_joint",
        "waist_yaw_joint",
        "right_hip_pitch_joint",
        "left_hip_pitch_joint",
    ],
    "left_arm": [
        "left_shoulder_pitch_joint",
        "left_shoulder_roll_joint",
        "left_shoulder_yaw_joint",
        "left_elbow_joint",
        "left_wrist_roll_joint",
        "left_wrist_yaw_joint",
        "left_wrist_pitch_joint",
    ],
    "right_arm": [
        "right_shoulder_pitch_joint",
        "right_shoulder_roll_joint",
        "right_shoulder_yaw_joint",
        "right_elbow_joint",
        "right_wrist_roll_joint",
        "right_wrist_yaw_joint",
        "right_wrist_pitch_joint",
    ],
    "right_short_arm": [
        "right_shoulder_pitch_joint",
        "right_shoulder_roll_joint",
        "right_shoulder_yaw_joint",
        "right_elbow_joint",
        "right_wrist_roll_joint",
    ],
    "left_short_arm": [
        "left_shoulder_pitch_joint",
        "left_shoulder_roll_joint",
        "left_shoulder_yaw_joint",
        "left_elbow_joint",
        "left_wrist_roll_joint",
    ],
    "upper_body": [
        "waist_roll_joint",
        "waist_yaw_joint",
        "left_shoulder_pitch_joint",
        "left_shoulder_roll_joint",
        "left_shoulder_yaw_joint",
        "left_elbow_joint",
        "left_wrist_roll_joint",
        "right_shoulder_pitch_joint",
        "right_shoulder_roll_joint",
        "right_shoulder_yaw_joint",
        "right_elbow_joint",
        "right_wrist_roll_joint",
    ],
    # right_leg(6) + left_leg(6) + waist_roll + waist_yaw
    "biped_waist": [
        "right_hip_pitch_joint",
        "right_hip_roll_joint",
        "right_hip_yaw_joint",
        "right_knee_joint",
        "right_ankle_pitch_joint",
        "right_ankle_roll_joint",
        "left_hip_pitch_joint",
        "left_hip_roll_joint",
        "left_hip_yaw_joint",
        "left_knee_joint",
        "left_ankle_pitch_joint",
        "left_ankle_roll_joint",
        "waist_roll_joint",
        "waist_yaw_joint",
    ],
}


@dataclass(frozen=True)
class MotorEntry:
    index: int
    motor_id: int
    interface_type: str
    interface: str
    motor_type: str
    motor_model: int
    master_id_offset: int
    zero_offset: float
    kp: float
    kd: float
    sign: float
    joint_name: str
    backend: str = "socketcan"


def add_motors_python_paths(root: Path = MOTORS_ROOT) -> None:
    """Make the bundled motors_py module importable."""

    candidates = [
        str(root / "build"),
        *glob.glob(str(root / "install" / "*" / "lib" / "python*" / "site-packages")),
        *glob.glob(str(root / "install" / "lib" / "python*" / "site-packages")),
        *glob.glob(str(root / "lib" / "python*" / "site-packages")),
        *glob.glob(str(root / "build" / "*")),
    ]
    for path in reversed(candidates):
        if Path(path).is_dir() and path not in sys.path:
            sys.path.insert(0, path)


def canonical_limb(name: str) -> str:
    key = name.strip().lower().replace("-", "_").replace(" ", "_")
    # lower_body is the hardware-facing alias of biped_waist (14-DoF).
    if key == "lower_body":
        key = "biped_waist"
    if key not in LIMB_JOINT_NAMES:
        valid = ", ".join(sorted([*LIMB_JOINT_NAMES, "lower_body"]))
        raise ValueError(f"unknown limb {name!r}; valid limbs: {valid}")
    return key


def limb_joint_names(limb: str) -> list[str]:
    return list(LIMB_JOINT_NAMES[canonical_limb(limb)])


def load_yaml_config(path: Path | str) -> dict[str, Any]:
    path = Path(path)
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a YAML mapping")
    return data


def _require_list(config: dict[str, Any], key: str) -> list[Any]:
    value = config.get(key)
    if not isinstance(value, list):
        raise ValueError(f"{key} must be a list")
    return value


def _exact_list(config: dict[str, Any], key: str, size: int, cast):
    values = _require_list(config, key)
    if len(values) != size:
        raise ValueError(f"{key} must contain {size} values, got {len(values)}")
    return [cast(value) for value in values]


def _bus_values(config: dict[str, Any], key: str, bus_count: int, cast) -> list[Any]:
    values = _require_list(config, key)
    if len(values) == 1:
        values = values * bus_count
    if len(values) != bus_count:
        raise ValueError(f"{key} must contain 1 or {bus_count} values, got {len(values)}")
    return [cast(value) for value in values]


def close_chain_motor_indices(config: dict[str, Any], limb: str, motor_count: int) -> tuple[int, int]:
    limb = canonical_limb(limb)
    if not limb.endswith("_leg"):
        raise ValueError(f"close-chain mapping only applies to legs, got {limb!r}")

    values = config.get("close_chain_motor_idx", [4, 5])
    if not isinstance(values, list):
        raise ValueError("close_chain_motor_idx must be a list")
    if len(values) != 2:
        raise ValueError(f"close_chain_motor_idx must contain 2 values, got {len(values)}")

    indices: list[int] = []
    for value in values:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("close_chain_motor_idx must contain integer indices")
        indices.append(value)

    if indices[0] == indices[1]:
        raise ValueError("close_chain_motor_idx values must be different")
    for index in indices:
        if index < 0 or index >= motor_count:
            raise ValueError(f"close_chain_motor_idx value {index} out of range for {motor_count} motors")
    return indices[0], indices[1]


def limb_motor_entries(config: dict[str, Any], limb: str) -> list[MotorEntry]:
    limb = canonical_limb(limb)
    joint_names = limb_joint_names(limb)
    motor_nums = _exact_list(config, "motor_num", len(_require_list(config, "motor_interface")), int)
    motor_count = sum(motor_nums)
    if motor_count != len(joint_names):
        raise ValueError(f"{limb} config must contain {len(joint_names)} motors, got {motor_count}")

    bus_count = len(motor_nums)
    motor_ids = _exact_list(config, "motor_id", motor_count, int)
    interfaces = _exact_list(config, "motor_interface", bus_count, str)
    interface_types = _bus_values(config, "motor_interface_type", bus_count, lambda value: str(value).lower())
    motor_types = _bus_values(config, "motor_type", bus_count, lambda value: str(value).upper())
    if "motor_backend" in config:
        raw_backends = config["motor_backend"]
        if isinstance(raw_backends, str):
            backend_config = dict(config)
            backend_config["motor_backend"] = [raw_backends]
        else:
            backend_config = config
        backends = _bus_values(
            backend_config,
            "motor_backend",
            bus_count,
            lambda value: str(value).lower(),
        )
    else:
        backends = ["socketcan"] * bus_count
    motor_models = _exact_list(config, "motor_model", motor_count, int)
    zero_offsets = _exact_list(config, "motor_zero_offset", motor_count, float)
    kp = _exact_list(config, "kp", motor_count, float)
    kd = _exact_list(config, "kd", motor_count, float)
    signs = _exact_list(config, "motor_sign", motor_count, float)
    master_id_offset = int(config.get("master_id_offset", 0))

    entries: list[MotorEntry] = []
    offset = 0
    for bus_index, count in enumerate(motor_nums):
        for local_index in range(count):
            motor_index = offset + local_index
            entries.append(
                MotorEntry(
                    index=motor_index,
                    motor_id=motor_ids[motor_index],
                    interface_type=interface_types[bus_index],
                    interface=interfaces[bus_index],
                    motor_type=motor_types[bus_index],
                    motor_model=motor_models[motor_index],
                    master_id_offset=master_id_offset,
                    zero_offset=zero_offsets[motor_index],
                    kp=kp[motor_index],
                    kd=kd[motor_index],
                    sign=signs[motor_index],
                    joint_name=joint_names[motor_index],
                    backend=backends[bus_index],
                )
            )
        offset += count
    return entries


def limb_motor_slot_names(config: dict[str, Any], limb: str, motor_count: int | None = None) -> list[str]:
    entries = limb_motor_entries(config, limb)
    if motor_count is not None and motor_count != len(entries):
        raise ValueError(f"{limb} expected {len(entries)} motor slots, got {motor_count}")
    return [entry.joint_name for entry in entries]


def print_motor_table(entries: list[MotorEntry]) -> None:
    print("idx  joint                         id  iface type  backend   bus       model  kp       kd       sign    zero")
    for entry in entries:
        print(
            f"{entry.index:>3}  {entry.joint_name:<28} "
            f"{entry.motor_id:>2}  {entry.interface_type:<5} {entry.motor_type:<4} "
            f"{entry.backend:<9} "
            f"{entry.interface:<8} {entry.motor_model:>5} "
            f"{entry.kp:>7.3f} {entry.kd:>7.3f} {entry.sign:>7.3f} {entry.zero_offset:>7.3f}"
        )


def create_motor_driver(entry: MotorEntry, motors_py: Any) -> Any:
    kwargs = dict(
        motor_id=entry.motor_id,
        interface_type=entry.interface_type,
        interface=entry.interface,
        motor_type=entry.motor_type,
        motor_model=entry.motor_model,
        master_id_offset=entry.master_id_offset,
        motor_zero_offset=entry.zero_offset,
    )
    try:
        return motors_py.MotorDriver.create_motor(
            **kwargs,
            backend=entry.backend,
        )
    except TypeError as exc:
        if entry.backend != "socketcan":
            raise RuntimeError(
                "the installed motors_py does not support a selectable CAN "
                "backend; rebuild motors with the Windows/ZLG changes"
            ) from exc
        # Backward compatibility with an already-built Linux motors_py from
        # before the optional backend argument was introduced.
        return motors_py.MotorDriver.create_motor(**kwargs)


def supports_batch_mit(interface_type: str, motor_type: str) -> bool:
    return interface_type.lower() in BATCH_MIT_INTERFACE_TYPES and motor_type.upper() in BATCH_MIT_MOTOR_TYPES


def batch_mit_slot(motor_id: int, motor_type: str) -> int:
    motor_type = motor_type.upper()
    if motor_type == "XYN":
        device_id = int(motor_id) & 0x7F
        if 1 <= device_id <= 7:
            return device_id - 1
        raise ValueError(f"XYN one-to-many MIT requires device id in [1, 7], got motor_id={motor_id}")

    if 1 <= int(motor_id) <= 8:
        return int(motor_id) - 1
    raise ValueError(f"{motor_type} one-to-many MIT requires motor_id in [1, 8], got {motor_id}")


def require_batch_mit_entry(entry: MotorEntry) -> int:
    if not supports_batch_mit(entry.interface_type, entry.motor_type):
        raise ValueError(
            f"{entry.interface_type}/{entry.motor_type} does not support one-to-many MIT commands"
        )
    return batch_mit_slot(entry.motor_id, entry.motor_type)


def coerce_2d(values: np.ndarray) -> np.ndarray:
    arr = np.asarray(values, dtype=np.float64)
    if arr.ndim == 1:
        arr = arr.reshape(1, -1)
    if arr.ndim != 2:
        raise ValueError(f"trajectory must be 1D or 2D, got shape {arr.shape}")
    if not np.all(np.isfinite(arr)):
        raise ValueError("trajectory contains NaN or inf")
    return arr


def load_motor_position_npz(path: Path | str) -> tuple[np.ndarray, float, str | None, np.ndarray]:
    path = Path(path)
    if path.suffix.lower() != ".npz":
        raise ValueError("trajectory must be a .npz file generated by record_sim_limb_traj.py")

    with np.load(path, allow_pickle=False) as data:
        if "motor_pos" not in data.files:
            raise ValueError(f"{path} missing required array 'motor_pos'")
        if "fps" not in data.files:
            raise ValueError(f"{path} missing required array 'fps'")

        motor_pos = coerce_2d(np.asarray(data["motor_pos"], dtype=np.float64))
        fps_arr = np.asarray(data["fps"], dtype=np.float64).reshape(-1)
        if fps_arr.size == 0 or fps_arr[0] <= 0 or not np.isfinite(fps_arr[0]):
            raise ValueError(f"{path} contains invalid fps")
        fps = float(fps_arr[0])

        if "time" in data.files:
            trajectory_time = np.asarray(data["time"], dtype=np.float64).reshape(-1)
            if trajectory_time.shape != (motor_pos.shape[0],):
                raise ValueError(
                    f"{path} time must contain {motor_pos.shape[0]} values, got {trajectory_time.size}"
                )
            if not np.all(np.isfinite(trajectory_time)):
                raise ValueError(f"{path} time contains NaN or inf")
            if trajectory_time.size > 1 and np.any(np.diff(trajectory_time) <= 0.0):
                raise ValueError(f"{path} time must be strictly increasing")
            trajectory_time = trajectory_time - float(trajectory_time[0])
        else:
            trajectory_time = np.arange(motor_pos.shape[0], dtype=np.float64) / fps

        limb = None
        if "limb" in data.files:
            limb_arr = np.asarray(data["limb"]).reshape(-1)
            if limb_arr.size:
                limb_value = limb_arr[0]
                if isinstance(limb_value, bytes):
                    limb = limb_value.decode("utf-8")
                else:
                    limb = str(limb_value)
        elif "limbs" in data.files:
            # Combined trajectories (e.g. right_leg + left_leg + waist).
            limbs_arr = [str(v) for v in np.asarray(data["limbs"]).reshape(-1)]
            if limbs_arr == ["right_leg", "left_leg", "waist"]:
                limb = "biped_waist"

    return motor_pos, fps, limb, trajectory_time


def trajectory_target_at(values: np.ndarray, timestamps: np.ndarray, timestamp: float) -> np.ndarray:
    values = coerce_2d(values)
    timestamps = np.asarray(timestamps, dtype=np.float64).reshape(-1)
    if timestamps.shape != (values.shape[0],):
        raise ValueError(f"timestamps must contain {values.shape[0]} values, got {timestamps.size}")

    if values.shape[0] == 1 or timestamp <= timestamps[0]:
        return values[0]
    if timestamp >= timestamps[-1]:
        return values[-1]

    right = int(np.searchsorted(timestamps, timestamp, side="right"))
    left = right - 1
    dt = float(timestamps[right] - timestamps[left])
    if dt <= 0.0:
        return values[left]
    alpha = (float(timestamp) - float(timestamps[left])) / dt
    return values[left] + (values[right] - values[left]) * alpha


def _leg_close_chain_idx_for_biped(config: dict[str, Any]) -> tuple[int, int]:
    """Per-leg close-chain indices inside each 6-motor leg block (default 4,5)."""

    values = config.get("close_chain_motor_idx", [4, 5])
    if not isinstance(values, list) or len(values) != 2:
        raise ValueError("close_chain_motor_idx must be a list of 2 indices for biped_waist")
    i0, i1 = int(values[0]), int(values[1])
    if i0 == i1 or min(i0, i1) < 0 or max(i0, i1) >= 6:
        raise ValueError("biped_waist close_chain_motor_idx must be distinct indices in [0, 5]")
    return i0, i1


def limb_joint_to_motor(joint_pos: np.ndarray, config: dict[str, Any], limb: str) -> np.ndarray:
    limb = canonical_limb(limb)
    entries = limb_motor_entries(config, limb)
    joint_pos = coerce_2d(joint_pos)
    if joint_pos.shape[1] != len(entries):
        raise ValueError(f"{limb} joint trajectory must have {len(entries)} columns, got {joint_pos.shape[1]}")

    if limb == "biped_waist":
        close_chain_idx = _leg_close_chain_idx_for_biped(config)
        right = np.vstack([leg_joint_to_motor(row, "right_leg", close_chain_idx) for row in joint_pos[:, 0:6]])
        left = np.vstack([leg_joint_to_motor(row, "left_leg", close_chain_idx) for row in joint_pos[:, 6:12]])
        waist = joint_pos[:, 12:14].copy()
        motor = np.concatenate([right, left, waist], axis=1)
    elif limb.endswith("_leg"):
        close_chain_idx = close_chain_motor_indices(config, limb, len(entries))
        motor = np.vstack([leg_joint_to_motor(row, limb, close_chain_idx) for row in joint_pos])
    else:
        motor = joint_pos.copy()
    motor *= np.array([entry.sign for entry in entries], dtype=np.float64)
    return motor


def limb_motor_to_joint(motor_pos: np.ndarray, config: dict[str, Any], limb: str) -> np.ndarray:
    limb = canonical_limb(limb)
    entries = limb_motor_entries(config, limb)
    motor_pos = coerce_2d(motor_pos)
    if motor_pos.shape[1] != len(entries):
        raise ValueError(f"{limb} motor trajectory must have {len(entries)} columns, got {motor_pos.shape[1]}")

    signs = np.array([entry.sign for entry in entries], dtype=np.float64)
    if np.any(np.abs(signs) <= 1e-12):
        raise ValueError("motor_sign values must be non-zero")
    joint = motor_pos / signs

    if limb == "biped_waist":
        close_chain_idx = _leg_close_chain_idx_for_biped(config)
        defaults = joint_default_positions(config, limb)
        right_initial = defaults[4:6].copy()
        left_initial = defaults[10:12].copy()
        for row in joint:
            row[4:6] = ankle_motor_to_joint(
                row[close_chain_idx[0]],
                row[close_chain_idx[1]],
                "right_leg",
                initial=right_initial,
            )
            right_initial = row[4:6].copy()
            left_block = row[6:12]
            left_block[4:6] = ankle_motor_to_joint(
                left_block[close_chain_idx[0]],
                left_block[close_chain_idx[1]],
                "left_leg",
                initial=left_initial,
            )
            left_initial = left_block[4:6].copy()
            row[6:12] = left_block
    elif limb.endswith("_leg"):
        close_chain_idx = close_chain_motor_indices(config, limb, len(entries))
        ankle_start = len(entries) - 2
        initial = joint_default_positions(config, limb)[ankle_start:].copy()
        for row in joint:
            row[ankle_start:] = ankle_motor_to_joint(
                row[close_chain_idx[0]],
                row[close_chain_idx[1]],
                limb,
                initial=initial,
            )
            initial = row[ankle_start:].copy()

    return joint


def joint_default_positions(config: dict[str, Any], limb: str) -> np.ndarray:
    limb = canonical_limb(limb)
    joint_count = len(limb_joint_names(limb))
    values = config.get("joint_default_angle")
    if values is None:
        return np.zeros(joint_count, dtype=np.float64)
    if not isinstance(values, list):
        raise ValueError("joint_default_angle must be a list")

    default = np.asarray([float(value) for value in values], dtype=np.float64)
    if default.shape != (joint_count,):
        raise ValueError(f"joint_default_angle must contain {joint_count} values for {limb}, got {default.size}")
    if not np.all(np.isfinite(default)):
        raise ValueError("joint_default_angle contains NaN or inf")
    return default


def motor_default_positions(config: dict[str, Any], limb: str) -> np.ndarray:
    return limb_joint_to_motor(joint_default_positions(config, limb), config, limb).reshape(-1)


def refresh_motor_feedback(motors: list[Any], *, settle_s: float = 0.010) -> None:
    """Request MIT status frames and wait briefly for async CAN RX to update caches."""
    for motor in motors:
        motor.refresh_motor_status()
    if settle_s > 0.0:
        time.sleep(settle_s)


def sync_motor_feedback(
    motors: list[Any],
    entries: list[MotorEntry] | None = None,
    *,
    settle_s: float = 0.020,
) -> None:
    """Query each motor with single-motor MIT refresh, then wait for CAN RX.

    Batch MIT with kp=kd=0 does not reliably populate feedback on all LRO
    firmware versions.
    """
    _ = entries
    if not motors:
        return
    per_motor_s = max(0.004, float(settle_s) / len(motors))
    for motor in motors:
        motor.refresh_motor_status()
        time.sleep(per_motor_s)


def hip_roll_grav_comp_enabled(config: dict[str, Any]) -> bool:
    cfg = config.get("hip_roll_grav_comp")
    if not isinstance(cfg, dict):
        return False
    return bool(cfg.get("enable", False))


# biped_waist joint-space indices for feedforward table keys (no "_joint" suffix).
_BIPED_FF_JOINT_INDEX: dict[str, int] = {
    "right_hip_pitch": 0,
    "right_hip_roll": 1,
    "right_hip_yaw": 2,
    "right_knee": 3,
    "right_ankle_pitch": 4,
    "right_ankle_roll": 5,
    "left_hip_pitch": 6,
    "left_hip_roll": 7,
    "left_hip_yaw": 8,
    "left_knee": 9,
    "left_ankle_pitch": 10,
    "left_ankle_roll": 11,
    "waist_roll": 12,
    "waist_yaw": 13,
}


def _ff_term(cfg: dict[str, Any], key: str, default: float = 0.0) -> float:
    value = cfg.get(key, default)
    return float(value)


def _legacy_biped_ff_table(cfg: dict[str, Any]) -> dict[str, dict[str, float]]:
    """Build joints table from older flat YAML keys if joints: is absent."""

    stance = _ff_term(cfg, "stance_bias_nm")
    swing_roll = _ff_term(cfg, "swing_bias_nm")
    waist_bias = _ff_term(cfg, "waist_roll_bias_nm")
    waist_off = _ff_term(cfg, "waist_roll_offset_nm")
    pitch = _ff_term(cfg, "hip_pitch_swing_bias_nm")
    return {
        "waist_roll": {"offset": waist_off, "swing_L": waist_bias, "swing_R": -waist_bias},
        "left_hip_roll": {"offset": 0.0, "swing_L": swing_roll, "swing_R": -stance},
        "right_hip_roll": {"offset": 0.0, "swing_L": stance, "swing_R": -swing_roll},
        "left_hip_pitch": {"offset": 0.0, "swing_L": pitch, "swing_R": 0.0},
        "right_hip_pitch": {"offset": 0.0, "swing_L": 0.0, "swing_R": -pitch},
    }


def biped_grav_ff_table(cfg: dict[str, Any]) -> dict[str, dict[str, float]]:
    raw = cfg.get("joints")
    if isinstance(raw, dict) and raw:
        table: dict[str, dict[str, float]] = {}
        for name, entry in raw.items():
            key = str(name).removesuffix("_joint")
            if key not in _BIPED_FF_JOINT_INDEX:
                raise ValueError(f"unknown hip_roll_grav_comp.joints key: {name}")
            if not isinstance(entry, dict):
                raise ValueError(f"hip_roll_grav_comp.joints.{name} must be a mapping")
            table[key] = {
                "offset": float(entry.get("offset", 0.0)),
                "swing_L": float(entry.get("swing_L", 0.0)),
                "swing_R": float(entry.get("swing_R", 0.0)),
            }
        return table
    return _legacy_biped_ff_table(cfg)


def compute_hip_roll_grav_torque(
    motor_target: np.ndarray,
    config: dict[str, Any],
    limb: str,
    entries: list[MotorEntry],
) -> np.ndarray:
    """Swing-gated gravity feedforward for biped_waist (motor-frame Nm).

    Per-joint model (joint space, before motor_sign):
      τ = offset + swing_L * swing_L_coef + swing_R * swing_R_coef
    swing_* from knee flexion vs joint_default_angle, clipped to [0,1].

    Coefficients are calibrated from datasave/RP1-LOWER-REL-001-170801-3.csv
    via least-squares on quasi-static samples (|spd|<0.25).
    """

    limb = canonical_limb(limb)
    n = len(entries)
    torque = np.zeros(n, dtype=np.float64)
    if limb != "biped_waist" or n != 14:
        return torque
    if not hip_roll_grav_comp_enabled(config):
        return torque

    cfg = config["hip_roll_grav_comp"]
    assert isinstance(cfg, dict)
    flex_ref = float(cfg.get("knee_flex_ref_rad", 1.0))
    if flex_ref <= 1e-6:
        raise ValueError("knee_flex_ref_rad must be > 0")

    joint = limb_motor_to_joint(np.asarray(motor_target, dtype=np.float64).reshape(1, -1), config, limb)[0]
    stand = joint_default_positions(config, limb)
    if stand.shape != (14,):
        raise ValueError("joint_default_angle must have 14 values for biped_waist grav-comp")

    swing_left = float(np.clip((float(stand[9]) - float(joint[9])) / flex_ref, 0.0, 1.0))
    swing_right = float(np.clip((float(joint[3]) - float(stand[3])) / flex_ref, 0.0, 1.0))

    tau_joint = np.zeros(14, dtype=np.float64)
    for name, coef in biped_grav_ff_table(cfg).items():
        idx = _BIPED_FF_JOINT_INDEX[name]
        tau_joint[idx] = (
            float(coef["offset"])
            + swing_left * float(coef["swing_L"])
            + swing_right * float(coef["swing_R"])
        )

    # Optional tiny sin(q) term on waist if requested (usually leave mass=0; table already fits data).
    waist_mass_kg = float(cfg.get("waist_roll_mass_kg", 0.0))
    waist_com_length_m = float(cfg.get("waist_roll_com_length_m", 0.15))
    if waist_mass_kg < 0.0 or waist_com_length_m < 0.0:
        raise ValueError("waist_roll_mass_kg / waist_roll_com_length_m must be >= 0")
    if waist_mass_kg > 0.0 and waist_com_length_m > 0.0:
        swing_any = max(swing_left, swing_right)
        tau_joint[12] += swing_any * (
            -waist_mass_kg * 9.81 * waist_com_length_m * math.sin(float(joint[12]))
        )

    for i, entry in enumerate(entries):
        torque[i] = float(tau_joint[i]) * float(entry.sign)
    if not np.all(np.isfinite(torque)):
        raise ValueError("biped gravity torque contains NaN or inf")
    return torque


def command_mit(
    motors: list[Any],
    entries: list[MotorEntry],
    target: np.ndarray,
    *,
    velocity: np.ndarray | None = None,
    kp_scale: float = 1.0,
    kd_scale: float = 1.0,
    torque: np.ndarray | None = None,
) -> None:
    if len(motors) != len(entries):
        raise ValueError(f"motors and entries length mismatch: {len(motors)} != {len(entries)}")

    target = np.asarray(target, dtype=np.float64).reshape(-1)
    if target.shape != (len(entries),):
        raise ValueError(f"MIT target must contain {len(entries)} values, got {target.size}")
    if not np.all(np.isfinite(target)):
        raise ValueError("MIT target contains NaN or inf")

    if velocity is None:
        velocity_vec = np.zeros(len(entries), dtype=np.float64)
    else:
        velocity_vec = np.asarray(velocity, dtype=np.float64).reshape(-1)
        if velocity_vec.shape != (len(entries),):
            raise ValueError(f"MIT velocity must contain {len(entries)} values, got {velocity_vec.size}")
        if not np.all(np.isfinite(velocity_vec)):
            raise ValueError("MIT velocity contains NaN or inf")

    if torque is None:
        torque_vec = np.zeros(len(entries), dtype=np.float64)
    else:
        torque_vec = np.asarray(torque, dtype=np.float64).reshape(-1)
        if torque_vec.shape != (len(entries),):
            raise ValueError(f"MIT torque must contain {len(entries)} values, got {torque_vec.size}")
        if not np.all(np.isfinite(torque_vec)):
            raise ValueError("MIT torque contains NaN or inf")

    kp_scale = float(kp_scale)
    kd_scale = float(kd_scale)
    if not math.isfinite(kp_scale) or not math.isfinite(kd_scale):
        raise ValueError("MIT gain scales must be finite")

    groups: dict[tuple[str, str, str], dict[str, Any]] = {}
    for motor, entry, pos, vel, tau in zip(motors, entries, target, velocity_vec, torque_vec):
        slot = require_batch_mit_entry(entry)
        key = (entry.interface_type.lower(), entry.interface, entry.motor_type.upper())
        group = groups.setdefault(
            key,
            {
                "motor": motor,
                "p": [0.0] * 8,
                "v": [0.0] * 8,
                "kp": [0.0] * 8,
                "kd": [0.0] * 8,
                "t": [0.0] * 8,
                "slots": set(),
            },
        )
        if slot in group["slots"]:
            raise ValueError(f"duplicate one-to-many MIT slot {slot} on {entry.interface}")
        group["slots"].add(slot)
        group["p"][slot] = float(pos)
        group["v"][slot] = float(vel)
        group["kp"][slot] = float(entry.kp) * kp_scale
        group["kd"][slot] = float(entry.kd) * kd_scale
        group["t"][slot] = float(tau)

    for group in groups.values():
        group["motor"].motors_mit_cmd(group["p"], group["v"], group["kp"], group["kd"], group["t"])


def positive_float(value: str) -> float:
    result = float(value)
    if result <= 0 or not math.isfinite(result):
        raise ValueError("value must be a positive finite number")
    return result
