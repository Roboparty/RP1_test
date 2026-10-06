from datetime import datetime, timezone

import pytest

from rp1_test.run_ingestion import _read_telemetry, _telemetry_series


def test_tracking_error_converts_rad_to_deg_and_skips_missing_or_nonfinite_pairs(tmp_path):
    telemetry = tmp_path / "telemetry.csv"
    telemetry.write_text(
        "time_s,motor_index,cmd_pos_rad,pos_rad\n"
        "0.00,0,0.1,0.09\n"
        "0.01,0,,0.09\n"
        "0.02,0,0.1,\n"
        "0.03,0,0.1,nan\n"
        "0.04,0,inf,0.09\n"
        "0.05,0,0.1,0.1\n",
        encoding="utf-8",
    )
    series = _telemetry_series(
        _read_telemetry(telemetry),
        [{"motor_index": 0, "telemetry_subject_code": "SARM-SHOULDER-PITCH"}],
        producer_execution_key="error-test",
        started_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        telemetry_path=telemetry,
        requested_stride=1,
        metric_version="1.0.0",
    )
    error = next(item for item in series if item["metric_code"] == "JOINT-TRACKING-ERROR")
    assert error["canonical_unit"] == "deg"
    assert [item["value"] for item in error["points"]] == pytest.approx([-0.5729577951308238, 0.0])
    assert [item["observed_at"] for item in error["points"]] == [
        "2026-01-01T00:00:00+00:00",
        "2026-01-01T00:00:00.050000+00:00",
    ]
    reference = error["raw_data_reference"]
    assert reference["derived"] is True
    assert reference["source_unit"] == "rad"
    assert "column" not in reference  # This is derived from two original CSV columns.


def test_tracking_error_is_omitted_when_no_row_has_both_positions(tmp_path):
    telemetry = tmp_path / "telemetry.csv"
    telemetry.write_text(
        "time_s,motor_index,cmd_pos_rad,pos_rad\n0.00,0,0.1,\n0.01,0,,0.09\n",
        encoding="utf-8",
    )
    series = _telemetry_series(
        _read_telemetry(telemetry),
        [{"motor_index": 0, "telemetry_subject_code": "SARM-SHOULDER-PITCH"}],
        producer_execution_key="missing-error-test",
        started_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        telemetry_path=telemetry,
        requested_stride=1,
        metric_version="1.0.0",
    )
    assert {item["metric_code"] for item in series} == {
        "JOINT-TARGET-POSITION",
        "JOINT-ACTUAL-POSITION",
    }
