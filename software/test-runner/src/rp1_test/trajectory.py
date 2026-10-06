"""Versioned trajectory contract with detached integrity metadata."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

SCHEMA_VERSION = "rp1-trajectory/v1"
REQUIRED_KEYS = {
    "schema_version",
    "limb",
    "joint_names",
    "motor_pos",
    "time",
    "fps",
    "model_fingerprint",
    "config_fingerprint",
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def fingerprint_file(path: Path | None) -> str:
    return sha256_file(path) if path is not None and path.is_file() else "not-provided"


def fingerprint_config(config: dict[str, Any]) -> str:
    encoded = json.dumps(config, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return hashlib.sha256(encoded).hexdigest()


def metadata_path(path: Path) -> Path:
    return path.with_suffix(".metadata.json")


@dataclass(frozen=True)
class Trajectory:
    path: Path
    schema_version: str
    limb: str
    joint_names: tuple[str, ...]
    motor_pos: np.ndarray
    time: np.ndarray
    fps: float
    model_fingerprint: str
    config_fingerprint: str


def write_trajectory(
    path: Path,
    *,
    limb: str,
    joint_names: list[str],
    motor_pos: np.ndarray,
    time: np.ndarray,
    fps: float,
    model_fingerprint: str,
    config_fingerprint: str,
    generated_by: str,
    generation: dict[str, Any] | None = None,
) -> Path:
    path = path.expanduser().resolve()
    if path.suffix.lower() != ".npz":
        raise ValueError("trajectory output must end in .npz")
    values = np.asarray(motor_pos, dtype=np.float64)
    timestamps = np.asarray(time, dtype=np.float64).reshape(-1)
    if values.ndim != 2 or values.shape[0] == 0:
        raise ValueError("motor_pos must be a non-empty 2D array")
    if values.shape[1] != len(joint_names):
        raise ValueError("motor_pos columns must match joint_names")
    if timestamps.shape != (values.shape[0],):
        raise ValueError("time length must match motor_pos rows")
    if timestamps[0] != 0.0 or (timestamps.size > 1 and np.any(np.diff(timestamps) <= 0)):
        raise ValueError("time must start at zero and be strictly increasing")
    if fps <= 0 or not np.all(np.isfinite(values)) or not np.all(np.isfinite(timestamps)):
        raise ValueError("trajectory values and fps must be finite and valid")

    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, suffix=".npz", delete=False) as handle:
        temp_path = Path(handle.name)
    try:
        np.savez(
            temp_path,
            schema_version=np.asarray([SCHEMA_VERSION]),
            limb=np.asarray([limb]),
            joint_names=np.asarray(joint_names),
            motor_pos=values,
            time=timestamps,
            fps=np.asarray([float(fps)], dtype=np.float64),
            model_fingerprint=np.asarray([model_fingerprint]),
            config_fingerprint=np.asarray([config_fingerprint]),
        )
        os.replace(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)

    metadata = {
        "schema_version": SCHEMA_VERSION,
        "npz_sha256": sha256_file(path),
        "npz_size_bytes": path.stat().st_size,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "generated_by": generated_by,
        "generation": generation or {},
    }
    metadata_path(path).write_text(
        json.dumps(metadata, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return path


def _scalar(data: Any, key: str) -> str:
    values = np.asarray(data[key]).reshape(-1)
    if values.size != 1:
        raise ValueError(f"{key} must contain exactly one value")
    return str(values[0])


def load_trajectory(path: Path, *, verify_metadata: bool = True) -> Trajectory:
    path = path.expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"trajectory not found: {path}")
    with np.load(path, allow_pickle=False) as data:
        missing = REQUIRED_KEYS.difference(data.files)
        if missing:
            raise ValueError(f"trajectory missing keys: {', '.join(sorted(missing))}")
        schema = _scalar(data, "schema_version")
        limb = _scalar(data, "limb")
        names = tuple(str(item) for item in np.asarray(data["joint_names"]).reshape(-1))
        values = np.asarray(data["motor_pos"], dtype=np.float64)
        timestamps = np.asarray(data["time"], dtype=np.float64).reshape(-1)
        fps_values = np.asarray(data["fps"], dtype=np.float64).reshape(-1)
        model_fp = _scalar(data, "model_fingerprint")
        config_fp = _scalar(data, "config_fingerprint")
    if schema != SCHEMA_VERSION:
        raise ValueError(f"unsupported schema_version: {schema}")
    if fps_values.size != 1 or fps_values[0] <= 0:
        raise ValueError("fps must contain one positive value")
    if values.ndim != 2 or values.shape != (timestamps.size, len(names)) or values.shape[0] == 0:
        raise ValueError("motor_pos/time/joint_names shape mismatch")
    if not np.all(np.isfinite(values)) or not np.all(np.isfinite(timestamps)):
        raise ValueError("trajectory contains NaN or inf")
    if timestamps[0] != 0.0 or (timestamps.size > 1 and np.any(np.diff(timestamps) <= 0)):
        raise ValueError("time must start at zero and be strictly increasing")
    if len(set(names)) != len(names):
        raise ValueError("joint_names must be unique")
    if verify_metadata:
        verify_trajectory_sha(path)
    return Trajectory(
        path=path,
        schema_version=schema,
        limb=limb,
        joint_names=names,
        motor_pos=values,
        time=timestamps,
        fps=float(fps_values[0]),
        model_fingerprint=model_fp,
        config_fingerprint=config_fp,
    )


def verify_trajectory_sha(path: Path) -> dict[str, Any]:
    sidecar = metadata_path(path)
    if not sidecar.is_file():
        raise FileNotFoundError(f"trajectory metadata not found: {sidecar}")
    metadata = json.loads(sidecar.read_text(encoding="utf-8"))
    actual_sha = sha256_file(path)
    actual_size = path.stat().st_size
    if metadata.get("npz_sha256") != actual_sha:
        raise ValueError("trajectory SHA-256 does not match metadata")
    if metadata.get("npz_size_bytes") != actual_size:
        raise ValueError("trajectory size does not match metadata")
    return metadata
