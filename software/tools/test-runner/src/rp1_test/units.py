"""The only angle-unit conversion boundary in the package."""

from __future__ import annotations

import math
from collections.abc import Iterable

import numpy as np


def deg_to_rad(value: float | Iterable[float]) -> float | np.ndarray:
    result = np.asarray(value, dtype=np.float64) * (math.pi / 180.0)
    return float(result) if result.ndim == 0 else result


def rad_to_deg(value: float | Iterable[float]) -> float | np.ndarray:
    result = np.asarray(value, dtype=np.float64) * (180.0 / math.pi)
    return float(result) if result.ndim == 0 else result


def require_radians(values: Iterable[float]) -> np.ndarray:
    result = np.asarray(values, dtype=np.float64)
    if not np.all(np.isfinite(result)):
        raise ValueError("angle values must be finite radians")
    return result
