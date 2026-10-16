"""Offline Qt trend ranges: small signals, channel selection, validity and history."""
import math

import pytest
from PySide6.QtWidgets import QApplication

from factory_hmi.desktop.plc_page import _TrendChart


@pytest.fixture
def chart():
    application = QApplication.instance() or QApplication([])
    widget = _TrendChart("电流趋势", "Current", "A", 50.0)
    widget.resize(560, 210)
    yield widget
    widget.deleteLater()
    assert application is not None


def sample(values, mask=1):
    return {"Current": values, "AnalogValidMask": mask}


def test_two_amp_fluctuations_use_a_small_range_without_forced_zero(chart):
    chart.set_samples([sample([value, 0, 0, 0]) for value in (1.98, 2.0, 2.02, 2.01)])
    lower, upper = chart.axis_range
    assert 0 < lower < 1.98 < 2.02 < upper < 2.5
    assert 0.2 - 1e-9 <= upper - lower <= 0.5
    assert not chart.grab().isNull()


def test_zero_channels_are_included_but_can_be_hidden_for_close_inspection(chart):
    data = [sample([2.0, 0, 0, 0], 15), sample([2.02, 0, 0, 0], 15)]
    chart.set_samples(data)
    assert chart.axis_range[0] == 0
    assert 2.02 <= chart.axis_range[1] <= 3.0
    for check in chart.channel_checks[1:]:
        check.setChecked(False)
    assert chart.axis_range[0] > 1.5
    assert chart.axis_range[1] - chart.axis_range[0] <= 0.5


def test_hiding_a_high_current_channel_excludes_it_from_scale(chart):
    chart.set_samples([sample([2, 30, 0, 0], 3), sample([2.03, 31, 0, 0], 3)])
    assert chart.axis_range[1] >= 31
    chart.channel_checks[1].setChecked(False)
    assert chart.axis_range[1] < 2.5


@pytest.mark.parametrize("invalid", [None, float("nan"), float("inf"), -1, 50.001, "bad"])
def test_invalid_samples_neither_distort_scale_nor_render_as_zero(chart, invalid):
    chart.set_samples([sample([2]), sample([invalid]), sample([2.02])])
    assert chart.axis_range[0] > 1.5
    assert chart._sample_value(sample([invalid]), 0) is None
    assert not chart.grab().isNull()


def test_invalid_mask_breaks_series_and_does_not_use_last_valid_or_extreme_value(chart):
    chart.set_samples([sample([2]), sample([50], 0), sample([2.02])])
    assert chart._sample_value(sample([50], 0), 0) is None
    assert chart.axis_range[1] < 2.5


def test_stable_ticks_do_not_jump_with_small_in_range_fluctuations(chart):
    chart.set_samples([sample([2]), sample([2.01])])
    original = chart.axis_range
    chart.set_samples([sample([2.005]), sample([2.015])])
    assert chart.axis_range == original
    chart.set_samples([sample([2.005]), sample([3])])
    assert chart.axis_range[1] > 3


def test_only_visible_recent_history_controls_scale_and_expired_outlier_shrinks(chart):
    chart.set_samples([sample([50]), sample([2])])
    assert chart.axis_range[1] == 50
    chart.set_samples([sample([50])] + [sample([2.01])] * 300)
    assert len(chart.samples) == 300
    assert chart.axis_range[1] < 2.5


@pytest.mark.parametrize("value", [0, 50])
def test_zero_and_full_scale_have_finite_nonzero_range(chart, value):
    chart.set_samples([sample([value]), sample([value])])
    low, high = chart.axis_range
    assert 0 <= low <= value <= high <= 50
    assert high > low
    assert all(math.isfinite(x) for x in (low, high))
    assert not chart.grab().isNull()


def test_no_valid_data_or_all_channels_hidden_is_safe(chart):
    chart.set_samples([sample([2]), sample([2.01])])
    for check in chart.channel_checks:
        check.setChecked(False)
    assert chart.axis_range == (0, 50)
    assert not chart.grab().isNull()
    chart.set_samples([sample([50], 0)])
    assert chart.axis_range == (0, 50)


def test_voltage_chart_zooms_around_operating_voltage_within_confirmed_50v_limit(chart):
    voltage = _TrendChart("电压趋势", "Voltage", "V", 50)
    try:
        voltage.set_samples([
            {"Voltage": [49.98], "AnalogValidMask": 1},
            {"Voltage": [50], "AnalogValidMask": 1},
        ])
        assert 48 <= voltage.axis_range[0] < 49.98
        assert voltage.axis_range[1] == 50
        assert voltage._sample_value({"Voltage": [50.001], "AnalogValidMask": 1}, 0) is None
    finally:
        voltage.deleteLater()
