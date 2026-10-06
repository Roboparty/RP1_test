from __future__ import annotations

import math
from dataclasses import dataclass

from scipy.stats import chi2


IMPLEMENTATION_KEY = "mtbf.poisson.exposure_estimate.v1"
IMPLEMENTATION_VERSION = "1.0.0"
DEFAULT_CONFIDENCE_LEVELS = (0.70, 0.90)


class MtbfInputError(ValueError):
    pass


@dataclass(frozen=True)
class MtbfResult:
    exposure_hours: float
    relevant_failure_count: int
    point_estimate_hours: float | None
    point_estimate_status: str
    lower_bounds: dict[float, float]

    def lower_bound(self, confidence: float) -> float:
        return self.lower_bounds[confidence]


def _validate(exposure_hours: float, failures: int, confidence: float | None = None) -> None:
    if not math.isfinite(exposure_hours) or exposure_hours < 0:
        raise MtbfInputError("exposure_hours must be a finite non-negative number")
    if isinstance(failures, bool) or not isinstance(failures, int) or failures < 0:
        raise MtbfInputError("failures must be a non-negative integer")
    if exposure_hours == 0 and failures > 0:
        raise MtbfInputError("positive failures require positive exposure")
    if confidence is not None and (not math.isfinite(confidence) or not 0 < confidence < 1):
        raise MtbfInputError("confidence must be between 0 and 1")


def lower_confidence_bound(exposure_hours: float, failures: int, confidence: float) -> float:
    _validate(exposure_hours, failures, confidence)
    if exposure_hours == 0:
        return 0.0
    if failures == 0:
        return exposure_hours / -math.log1p(-confidence)
    quantile = float(chi2.ppf(confidence, 2 * (failures + 1)))
    value = 2.0 * exposure_hours / quantile
    if not math.isfinite(value):
        raise MtbfInputError("calculation produced a non-finite confidence bound")
    return value


def calculate_mtbf(
    exposure_hours: float,
    failures: int,
    confidence_levels: tuple[float, ...] | list[float] = DEFAULT_CONFIDENCE_LEVELS,
) -> MtbfResult:
    _validate(exposure_hours, failures)
    levels = tuple(float(level) for level in confidence_levels)
    if not levels:
        raise MtbfInputError("at least one confidence level is required")
    for level in levels:
        _validate(exposure_hours, failures, level)

    if exposure_hours == 0:
        status = "NO_EXPOSURE"
    elif failures == 0:
        status = "NO_FINITE_ESTIMATE"
    else:
        status = "FINITE_ESTIMATE"

    return MtbfResult(
        exposure_hours=exposure_hours,
        relevant_failure_count=failures,
        point_estimate_hours=exposure_hours / failures if failures > 0 else None,
        point_estimate_status=status,
        lower_bounds={level: lower_confidence_bound(exposure_hours, failures, level) for level in levels},
    )
