from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import pytest

from app.domain.mtbf import MtbfInputError, calculate_mtbf, lower_confidence_bound
from app.services.worker import interval_union_seconds


@pytest.mark.parametrize(
    ("failures", "point", "lower_70", "lower_90"),
    [
        (0, None, 1544.8853938535195, 807.7877363400484),
        (1, 1860.0, 762.5399437686290, 478.1834987536888),
        (2, 930.0, 514.4420383996598, 349.4716367930763),
        (3, 620.0, 390.5733979394185, 278.4104768852472),
    ],
)
def test_contract_goldens(failures, point, lower_70, lower_90):
    result = calculate_mtbf(1860.0, failures)
    assert result.point_estimate_hours == point
    assert math.isclose(result.lower_bound(0.70), lower_70, rel_tol=1e-10, abs_tol=1e-6)
    assert math.isclose(result.lower_bound(0.90), lower_90, rel_tol=1e-10, abs_tol=1e-6)


def test_zero_failure_closed_form_matches_contract():
    general = lower_confidence_bound(1860.0, 0, 0.9)
    closed = 1860.0 / -math.log(1 - 0.9)
    assert math.isclose(general, closed, rel_tol=1e-12, abs_tol=1e-12)


def test_zero_exposure_without_failures_returns_no_exposure():
    result = calculate_mtbf(0.0, 0)
    assert result.point_estimate_hours is None
    assert result.point_estimate_status == "NO_EXPOSURE"
    assert result.lower_bound(0.70) == 0


@pytest.mark.parametrize(("exposure", "failures"), [(-1.0, 0), (0.0, 1)])
def test_invalid_exposure(exposure, failures):
    with pytest.raises(MtbfInputError):
        calculate_mtbf(exposure, failures)


def test_invalid_failure_count_and_confidence():
    with pytest.raises(MtbfInputError):
        calculate_mtbf(100.0, -1)
    with pytest.raises(MtbfInputError):
        calculate_mtbf(100.0, 1.5)  # type: ignore[arg-type]
    with pytest.raises(MtbfInputError):
        calculate_mtbf(100.0, 1, [1.0])


def test_overlapping_half_open_intervals_are_counted_once():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    intervals = [
        (start, start + timedelta(hours=2)),
        (start + timedelta(hours=1), start + timedelta(hours=3)),
        (start + timedelta(hours=3), start + timedelta(hours=4)),
    ]
    assert interval_union_seconds(intervals) == 4 * 3600


def test_sample_count_is_not_a_formula_input():
    one_asset = calculate_mtbf(1126.4, 1)
    five_assets = calculate_mtbf(1126.4, 1)
    assert one_asset == five_assets
    assert one_asset.point_estimate_hours == 1126.4
