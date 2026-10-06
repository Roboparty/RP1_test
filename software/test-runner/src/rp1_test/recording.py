"""MuJoCo-backed joint-keyframe and Cartesian-IK recording."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from .interpolation import interpolate_keyframes


def _require_mujoco():
    try:
        import mujoco
    except ImportError as exc:
        raise RuntimeError(
            "MuJoCo is required for trajectory record; install the 'sim' extra"
        ) from exc
    return mujoco


def _load_rows(path: Path) -> np.ndarray:
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = payload.get("keyframes", payload.get("targets")) if isinstance(payload, dict) else payload
    values = np.asarray(rows, dtype=np.float64)
    if values.ndim != 2 or values.shape[0] == 0 or not np.all(np.isfinite(values)):
        raise ValueError(f"{path} must contain a non-empty 2D numeric array")
    return values


def _joint_ids(mujoco, model, joint_names: list[str]) -> list[int]:
    ids = []
    for name in joint_names:
        joint_id = int(mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_JOINT, name))
        if joint_id < 0:
            raise ValueError(f"joint {name!r} not found in MuJoCo model")
        ids.append(joint_id)
    return ids


def record_joint_keyframes(
    model_path: Path,
    keyframes_path: Path,
    joint_names: list[str],
    *,
    fps: float,
    segment_seconds: float,
) -> tuple[np.ndarray, np.ndarray]:
    mujoco = _require_mujoco()
    model = mujoco.MjModel.from_xml_path(str(model_path))
    _joint_ids(mujoco, model, joint_names)
    keyframes = _load_rows(keyframes_path)
    if keyframes.shape[1] != len(joint_names):
        raise ValueError("joint keyframe width must match joint_names")
    return interpolate_keyframes(keyframes, fps=fps, segment_seconds=segment_seconds)


def record_ik_keyframes(
    model_path: Path,
    targets_path: Path,
    joint_names: list[str],
    *,
    site_name: str,
    fps: float,
    segment_seconds: float,
    damping: float = 1e-3,
    iterations: int = 80,
) -> tuple[np.ndarray, np.ndarray]:
    mujoco = _require_mujoco()
    model = mujoco.MjModel.from_xml_path(str(model_path))
    data = mujoco.MjData(model)
    joint_ids = _joint_ids(mujoco, model, joint_names)
    site_id = int(mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_SITE, site_name))
    if site_id < 0:
        raise ValueError(f"IK site {site_name!r} not found in MuJoCo model")
    targets = _load_rows(targets_path)
    if targets.shape[1] != 3:
        raise ValueError("IK targets must contain XYZ triples")
    dof_ids = np.asarray([model.jnt_dofadr[item] for item in joint_ids], dtype=np.int64)
    qpos_ids = np.asarray([model.jnt_qposadr[item] for item in joint_ids], dtype=np.int64)
    solved: list[np.ndarray] = []
    for target in targets:
        for _ in range(iterations):
            mujoco.mj_forward(model, data)
            error = target - np.asarray(data.site_xpos[site_id])
            if float(np.linalg.norm(error)) < 2e-4:
                break
            jacobian = np.zeros((3, model.nv), dtype=np.float64)
            mujoco.mj_jacSite(model, data, jacobian, None, site_id)
            jac = jacobian[:, dof_ids]
            step = jac.T @ np.linalg.solve(
                jac @ jac.T + damping * damping * np.eye(3), error
            )
            for index, joint_id in enumerate(joint_ids):
                value = float(data.qpos[qpos_ids[index]] + np.clip(step[index], -0.05, 0.05))
                if model.jnt_limited[joint_id]:
                    value = float(np.clip(value, *model.jnt_range[joint_id]))
                data.qpos[qpos_ids[index]] = value
        else:
            raise RuntimeError(f"IK failed to converge for target {target.tolist()}")
        solved.append(np.asarray(data.qpos[qpos_ids], dtype=np.float64).copy())
    return interpolate_keyframes(
        np.asarray(solved), fps=fps, segment_seconds=segment_seconds
    )
