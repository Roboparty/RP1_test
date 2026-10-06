"""Extensible, parameterized trajectory generators."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import numpy as np

from .interpolation import interpolate_keyframes

Generator = Callable[[list[str], float, dict[str, Any]], tuple[np.ndarray, np.ndarray]]
GENERATORS: dict[str, Generator] = {}


def register(name: str) -> Callable[[Generator], Generator]:
    def decorator(function: Generator) -> Generator:
        if name in GENERATORS:
            raise ValueError(f"generator already registered: {name}")
        GENERATORS[name] = function
        return function
    return decorator


def _base(params: dict[str, Any], size: int) -> np.ndarray:
    values = np.asarray(params.get("default", [0.0] * size), dtype=np.float64)
    if values.shape != (size,):
        raise ValueError(f"default must contain {size} values")
    return values


@register("minimum-jerk")
def generic_minimum_jerk(
    joint_names: list[str], fps: float, params: dict[str, Any]
) -> tuple[np.ndarray, np.ndarray]:
    start = np.asarray(params.get("start", [0.0] * len(joint_names)), dtype=np.float64)
    end = np.asarray(params.get("end", [0.0] * len(joint_names)), dtype=np.float64)
    if start.shape != (len(joint_names),) or end.shape != start.shape:
        raise ValueError("start/end must match joint_names")
    return interpolate_keyframes(
        np.vstack([start, end]),
        fps=fps,
        segment_seconds=float(params.get("duration", 2.0)),
    )


@register("joint-inspect")
def joint_inspect(
    joint_names: list[str], fps: float, params: dict[str, Any]
) -> tuple[np.ndarray, np.ndarray]:
    base = _base(params, len(joint_names))
    selected = str(params.get("joint", joint_names[0]))
    if selected not in joint_names:
        raise ValueError(f"joint-inspect joint not found: {selected}")
    amplitude = float(params.get("amplitude_rad", 0.2))
    target = base.copy()
    target[joint_names.index(selected)] += amplitude
    return interpolate_keyframes(
        np.vstack([base, target, base]),
        fps=fps,
        segment_seconds=float(params.get("segment_seconds", 1.0)),
    )


@register("dumbbell-arm")
def dumbbell_arm(
    joint_names: list[str], fps: float, params: dict[str, Any]
) -> tuple[np.ndarray, np.ndarray]:
    base = _base(params, len(joint_names))
    lift = base.copy()
    for token, delta in (
        ("shoulder_pitch", float(params.get("shoulder_pitch_rad", -0.45))),
        ("shoulder_roll", float(params.get("shoulder_roll_rad", 0.25))),
        ("elbow", float(params.get("elbow_rad", -0.8))),
    ):
        for index, name in enumerate(joint_names):
            if token in name:
                lift[index] += delta
    return interpolate_keyframes(
        np.vstack([base, lift, base]),
        fps=fps,
        segment_seconds=float(params.get("segment_seconds", 1.5)),
    )


@register("biped-waist")
def biped_waist(
    joint_names: list[str], fps: float, params: dict[str, Any]
) -> tuple[np.ndarray, np.ndarray]:
    base = _base(params, len(joint_names))
    left = base.copy()
    right = base.copy()
    sway = float(params.get("waist_sway_rad", 0.12))
    knee = float(params.get("knee_flex_rad", 0.25))
    for index, name in enumerate(joint_names):
        if name == "waist_roll_joint":
            left[index], right[index] = sway, -sway
        elif "knee" in name:
            left[index] += knee
            right[index] += knee
    return interpolate_keyframes(
        np.vstack([base, left, right, base]),
        fps=fps,
        segment_seconds=float(params.get("segment_seconds", 1.0)),
    )


def generate(
    profile: str,
    joint_names: list[str],
    *,
    fps: float,
    params: dict[str, Any] | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    try:
        generator = GENERATORS[profile]
    except KeyError as exc:
        raise ValueError(f"unknown generator {profile!r}; available: {', '.join(sorted(GENERATORS))}") from exc
    return generator(joint_names, float(fps), params or {})
