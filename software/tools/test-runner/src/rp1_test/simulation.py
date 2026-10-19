"""MuJoCo replay that keeps dry-run independent of optional simulation assets."""

from __future__ import annotations

import os
import time
from pathlib import Path

from .trajectory import Trajectory


def resolve_model_path(explicit: Path | None) -> Path | None:
    if explicit is not None:
        return explicit.expanduser().resolve()
    root = os.environ.get("RP1_DESCRIPTION_ROOT", "").strip()
    if not root:
        return None
    base = Path(root).expanduser()
    for relative in ("mjcf/rp1_flat.xml", "mjcf/robot.xml", "rp1_flat.xml"):
        candidate = base / relative
        if candidate.is_file():
            return candidate.resolve()
    return None


def replay_summary(trajectory: Trajectory, model_path: Path | None) -> dict[str, object]:
    return {
        "limb": trajectory.limb,
        "frames": int(trajectory.motor_pos.shape[0]),
        "joints": int(trajectory.motor_pos.shape[1]),
        "fps": trajectory.fps,
        "duration_s": float(trajectory.time[-1]),
        "model": str(model_path) if model_path is not None else None,
        "model_available": bool(model_path and model_path.is_file()),
    }


def replay(
    trajectory: Trajectory,
    *,
    model_path: Path | None,
    loop: bool,
    cycles: int,
    headless: bool,
) -> None:
    if model_path is None or not model_path.is_file():
        raise RuntimeError(
            "rp1_description MuJoCo model is unavailable; pass --model or set RP1_DESCRIPTION_ROOT"
        )
    try:
        import mujoco
    except ImportError as exc:
        raise RuntimeError("MuJoCo is unavailable; install with: pip install -e '.[sim]'") from exc

    model = mujoco.MjModel.from_xml_path(str(model_path))
    data = mujoco.MjData(model)
    joint_ids: list[int] = []
    for name in trajectory.joint_names:
        joint_id = int(mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_JOINT, name))
        if joint_id < 0:
            raise ValueError(f"trajectory joint {name!r} not found in model")
        joint_ids.append(joint_id)

    viewer = None
    if not headless:
        try:
            import mujoco.viewer
            viewer = mujoco.viewer.launch_passive(model, data)
        except Exception as exc:
            raise RuntimeError(f"failed to open MuJoCo viewer: {exc}") from exc

    repeat = cycles if loop and cycles > 0 else (2**31 - 1 if loop else 1)
    try:
        for _ in range(repeat):
            start = time.monotonic()
            for frame, target_time in zip(trajectory.motor_pos, trajectory.time):
                for joint_id, value in zip(joint_ids, frame):
                    data.qpos[int(model.jnt_qposadr[joint_id])] = float(value)
                    data.qvel[int(model.jnt_dofadr[joint_id])] = 0.0
                mujoco.mj_forward(model, data)
                if viewer is not None:
                    if not viewer.is_running():
                        return
                    viewer.sync()
                delay = start + float(target_time) - time.monotonic()
                if delay > 0:
                    time.sleep(delay)
    finally:
        if viewer is not None:
            viewer.close()
