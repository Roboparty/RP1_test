"""Trajectory interpolation and transition helpers."""

from __future__ import annotations

import numpy as np


def minimum_jerk(alpha: float | np.ndarray) -> np.ndarray:
    value = np.clip(np.asarray(alpha, dtype=np.float64), 0.0, 1.0)
    return 10.0 * value**3 - 15.0 * value**4 + 6.0 * value**5


def interpolate_keyframes(
    keyframes: np.ndarray,
    *,
    fps: float,
    segment_seconds: float,
) -> tuple[np.ndarray, np.ndarray]:
    frames = np.asarray(keyframes, dtype=np.float64)
    if frames.ndim != 2 or frames.shape[0] == 0:
        raise ValueError("keyframes must be a non-empty 2D array")
    if fps <= 0 or segment_seconds <= 0:
        raise ValueError("fps and segment_seconds must be positive")
    if not np.all(np.isfinite(frames)):
        raise ValueError("keyframes contain NaN or inf")
    if frames.shape[0] == 1:
        return frames.copy(), np.asarray([0.0])
    count = max(2, int(round(fps * segment_seconds)))
    rows: list[np.ndarray] = []
    for start, end in zip(frames[:-1], frames[1:]):
        alpha = np.arange(count, dtype=np.float64) / count
        rows.extend(start + minimum_jerk(alpha)[:, None] * (end - start))
    rows.append(frames[-1].copy())
    result = np.asarray(rows, dtype=np.float64)
    return result, np.arange(result.shape[0], dtype=np.float64) / fps


def target_at(values: np.ndarray, timestamps: np.ndarray, timestamp: float) -> np.ndarray:
    points = np.asarray(values, dtype=np.float64)
    times = np.asarray(timestamps, dtype=np.float64).reshape(-1)
    if points.ndim != 2 or times.shape != (points.shape[0],):
        raise ValueError("values/time shape mismatch")
    if timestamp <= times[0] or points.shape[0] == 1:
        return points[0].copy()
    if timestamp >= times[-1]:
        return points[-1].copy()
    right = int(np.searchsorted(times, timestamp, side="right"))
    left = right - 1
    alpha = (timestamp - times[left]) / (times[right] - times[left])
    return points[left] + alpha * (points[right] - points[left])


def transition(q0: np.ndarray, q1: np.ndarray, *, fps: float, seconds: float) -> np.ndarray:
    start = np.asarray(q0, dtype=np.float64).reshape(-1)
    end = np.asarray(q1, dtype=np.float64).reshape(-1)
    if start.shape != end.shape or fps <= 0 or seconds <= 0:
        raise ValueError("invalid transition arguments")
    count = max(2, int(round(fps * seconds)))
    alpha = np.linspace(0.0, 1.0, count, endpoint=True)
    return start + minimum_jerk(alpha)[:, None] * (end - start)
