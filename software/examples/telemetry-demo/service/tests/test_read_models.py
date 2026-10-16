from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.core.errors import ApiError
from app.repositories.read_models import (
    JointStatisticPoint,
    JointStatisticSeries,
    ReadModelRepository,
    _context_is_stale,
    _decode_cursor,
    _downsample_points,
    _encode_cursor,
    _freshness,
    _is_source_warning,
    _live_elapsed_seconds,
    _resolve_joint_selection,
    _source_health_status,
    calculate_joint_summary,
)
from app.schemas.read_models import (
    DurationTotals,
    HomeObjectGroup,
    HomeScope,
    HomeTestCase,
    MtbfConclusionItem,
    MtbfObservedSection,
    MtbfStatisticalSection,
    MtbfVerifiedSection,
)


def test_cursor_round_trip_and_invalid_cursor_contract():
    cursor = _encode_cursor(42, 99)
    assert _decode_cursor(cursor, 2) == (42, 99)
    with pytest.raises(ApiError) as error:
        _decode_cursor("not-a-cursor", 2)
    assert error.value.code == "invalid_cursor"
    assert error.value.status_code == 422


def test_empty_freshness_has_explicit_no_data_state():
    now = datetime(2026, 9, 17, tzinfo=timezone.utc)
    value = _freshness(now, None)
    assert value.status == "NO_DATA"
    assert value.last_received_at is None
    assert value.lag_seconds is None
    assert value.as_of_at == now
    assert value.data_cutoff_at is None
    health_cases = [
        ("ACTIVE", "ACCEPTED", None, 30, "FRESH"),
        ("ACTIVE", "DUPLICATE", None, 30, "FRESH"),
        ("DEGRADED", "ACCEPTED", None, 30, "DEGRADED"),
        ("ACTIVE", "REJECTED", "invalid payload", 30, "ERROR"),
        ("ACTIVE", "PARTIAL", None, 30, "ERROR"),
        ("DISABLED", "REJECTED", "disabled source", 30, "DISABLED"),
        ("ACTIVE", None, None, None, "NO_DATA"),
    ]
    for configured, receipt, error, lag, expected in health_cases:
        assert _source_health_status(configured, receipt, error, lag) == expected
    assert _is_source_warning("DISABLED") is False
    assert _is_source_warning("DEGRADED") is True


def test_stale_running_elapsed_stops_at_last_heartbeat():
    now = datetime(2026, 9, 17, 12, tzinfo=timezone.utc)
    started = now - timedelta(hours=2)
    stale_heartbeat = now - timedelta(hours=1)
    assert _live_elapsed_seconds(now, started, stale_heartbeat, "RUNNING") == 3600
    assert _live_elapsed_seconds(now, started, None, "RUNNING") == 0
    assert _live_elapsed_seconds(now, started, now, "COMPLETED") == 0
    assert _context_is_stale(now, "RUNNING", None) is True
    assert _context_is_stale(now, "PAUSED", None) is False


def test_home_group_rejects_whole_machine_module_mixing():
    module = HomeScope(
        id="MODULE:SARM",
        code="SARM",
        name="单臂",
        asset_kind="MODULE",
        target_part_code="SARM",
        target_part_name="单臂",
        status="NOT_STARTED",
        part_durations=DurationTotals(),
    )
    with pytest.raises(ValidationError):
        HomeObjectGroup(asset_kind="WHOLE_MACHINE", objects=[module])


def test_home_object_rejects_test_case_from_another_target_part():
    with pytest.raises(ValidationError):
        HomeScope(
            id="MODULE:SLEG",
            code="SLEG",
            name="单腿",
            asset_kind="MODULE",
            target_part_code="SLEG",
            target_part_name="单腿",
            status="NOT_STARTED",
            part_durations=DurationTotals(),
            test_cases=[
                HomeTestCase(
                    id=uuid4(),
                    code="REL-SARM-001",
                    name="单臂用例",
                    target_part_code="SARM",
                    target_part_name="单臂",
                    status="NOT_STARTED",
                )
            ],
        )


def test_home_object_rejects_part_total_that_differs_from_case_sum():
    with pytest.raises(ValidationError):
        HomeScope(
            id="MODULE:SLEG",
            code="SLEG",
            name="单腿",
            asset_kind="MODULE",
            target_part_code="SLEG",
            target_part_name="单腿",
            status="COMPLETED",
            part_durations=DurationTotals(total_duration_seconds=99),
            test_cases=[
                HomeTestCase(
                    id=uuid4(),
                    code="REL-SLEG-001",
                    name="单腿用例",
                    target_part_code="SLEG",
                    target_part_name="单腿",
                    status="COMPLETED",
                    cumulative_duration_seconds=100,
                )
            ],
        )


def test_zero_failure_conclusion_rejects_point_estimate():
    with pytest.raises(ValidationError):
        MtbfConclusionItem(
            campaign_id=uuid4(),
            campaign_code="C-01",
            campaign_name="零故障测试",
            asset_kind="WHOLE_MACHINE",
            scope="OVERALL",
            scope_name="整体 MTBF",
            observed=MtbfObservedSection(
                effective_exposure_seconds=3600,
                relevant_failure_count=0,
                pending_failure_count=0,
                pending_exposure_seconds=0,
                excluded_seconds=0,
            ),
            statistical=MtbfStatisticalSection(
                method="mtbf.poisson.exposure_estimate.v1",
                point_estimate_hours=1,
                point_estimate_status="NO_FINITE_ESTIMATE",
                lower_bounds_hours={"0.9": 0.43},
                calculation_status="CURRENT",
            ),
            verified=MtbfVerifiedSection(status="NOT_AVAILABLE"),
        )


def _joint_series(
    metric_code: str,
    values: list[float | None],
    *,
    unit: str = "unit",
    point_qualities: list[str] | None = None,
    series_quality: str = "VALID",
) -> JointStatisticSeries:
    started_at = datetime(2026, 9, 18, tzinfo=timezone.utc)
    qualities = point_qualities or ["VALID"] * len(values)
    return JointStatisticSeries(
        metric_code=metric_code,
        unit=unit,
        quality_status=series_quality,
        started_at=started_at,
        ended_at=started_at + timedelta(seconds=10),
        points=tuple(
            JointStatisticPoint(
                sample_index=index,
                value=value,
                observed_at=started_at + timedelta(seconds=index),
                quality_status=qualities[index],
            )
            for index, value in enumerate(values)
        ),
    )


def _summary_by_code(series: list[JointStatisticSeries]):
    return {item.code: item for item in calculate_joint_summary(series)}


def test_joint_summary_calculates_fixed_backend_statistics():
    summary = _summary_by_code(
        [
            _joint_series(
                "JOINT-TRACKING-ERROR",
                [float(value) for value in range(1, 11)],
                unit="deg",
            ),
            _joint_series("JOINT-TORQUE", [-2, 4], unit="N.m"),
            _joint_series("JOINT-CURRENT", [1, 3], unit="A"),
            _joint_series("JOINT-TEMPERATURE", [20, 25], unit="degC"),
            _joint_series("JOINT-VIBRATION-RMS", [0.1, 0.3], unit="g"),
        ]
    )

    assert summary["TRACKING_ERROR_START_RMS"].value == 1
    assert summary["TRACKING_ERROR_END_RMS"].value == 10
    assert summary["TRACKING_ERROR_CHANGE_RATE"].value == 900
    assert summary["TRACKING_ERROR_P95_ABS"].value == pytest.approx(9.55)
    assert summary["TORQUE_MEAN_ABS"].value == 3
    assert summary["TORQUE_PEAK_ABS"].value == 4
    assert summary["CURRENT_MEAN"].value == 2
    assert summary["CURRENT_PEAK"].value == 3
    assert summary["TEMPERATURE_INITIAL"].value == 20
    assert summary["TEMPERATURE_FINAL"].value == 25
    assert summary["TEMPERATURE_RISE"].value == 5
    assert summary["VIBRATION_MAX"].value == 0.3
    assert summary["CYCLE_DURATION"].value == 10


def test_joint_summary_handles_missing_partial_and_zero_division():
    summary = _summary_by_code(
        [
            _joint_series(
                "JOINT-TRACKING-ERROR",
                [0] + [1] * 9,
                unit="deg",
            ),
            _joint_series(
                "JOINT-TORQUE",
                [1, None, 3],
                unit="N.m",
                point_qualities=["VALID", "MISSING", "PARTIAL"],
                series_quality="PARTIAL",
            ),
        ]
    )

    assert summary["TRACKING_ERROR_CHANGE_RATE"].value is None
    assert (
        summary["TRACKING_ERROR_CHANGE_RATE"].quality_status
        == "NOT_CALCULABLE"
    )
    assert summary["TORQUE_MEAN_ABS"].value == 2
    assert summary["TORQUE_MEAN_ABS"].sample_count == 2
    assert summary["TORQUE_MEAN_ABS"].quality_status == "PARTIAL"
    assert summary["CURRENT_MEAN"].value is None
    assert summary["CURRENT_MEAN"].quality_status == "NO_DATA"


def test_joint_series_downsampling_caps_points_and_preserves_endpoints():
    points = [SimpleNamespace(sample_index=index) for index in range(1_000)]
    displayed = _downsample_points(points)
    assert len(displayed) == 120
    assert displayed[0].sample_index == 0
    assert displayed[-1].sample_index == 999
    assert [point.sample_index for point in displayed] == sorted(
        point.sample_index for point in displayed
    )


def test_joint_selection_rejects_filter_when_execution_has_no_series():
    with pytest.raises(ApiError) as error:
        _resolve_joint_selection(
            [],
            subject_code="J999",
            stage_code=None,
            cycle_index=None,
        )
    assert error.value.status_code == 422
    assert error.value.code == "invalid_joint_analysis_selection"
    assert error.value.details[0]["field"] == "subject_code"


def test_joint_selection_defaults_and_rejects_unavailable_dimensions():
    metadata = [
        SimpleNamespace(
            subject_code="J2",
            subject_display_order=2,
            stage_code="STEADY_RUN",
            stage_sequence_no=2,
            cycle_index=1,
        ),
        SimpleNamespace(
            subject_code="J1",
            subject_display_order=1,
            stage_code="RAMP_UP",
            stage_sequence_no=1,
            cycle_index=0,
        ),
        SimpleNamespace(
            subject_code="J1",
            subject_display_order=1,
            stage_code="STEADY_RUN",
            stage_sequence_no=2,
            cycle_index=1,
        ),
        SimpleNamespace(
            subject_code="J1",
            subject_display_order=1,
            stage_code="STEADY_RUN",
            stage_sequence_no=2,
            cycle_index=2,
        ),
    ]
    selection = _resolve_joint_selection(
        metadata,
        subject_code=None,
        stage_code=None,
        cycle_index=None,
    )
    assert selection.subject_code == "J1"
    assert selection.stage_code == "STEADY_RUN"
    assert selection.cycle_index == 2

    for filters, field in (
        ({"subject_code": "J9", "stage_code": None, "cycle_index": None}, "subject_code"),
        ({"subject_code": "J1", "stage_code": "COOL_DOWN", "cycle_index": None}, "stage_code"),
        ({"subject_code": "J1", "stage_code": "STEADY_RUN", "cycle_index": 9}, "cycle_index"),
    ):
        with pytest.raises(ApiError) as error:
            _resolve_joint_selection(metadata, **filters)
        assert error.value.status_code == 422
        assert error.value.details[0]["field"] == field


def test_joint_execution_resolution_preserves_rls_not_found_semantics():
    class EmptyResult:
        def mappings(self):
            return self

        def first(self):
            return None

    class EmptySession:
        def execute(self, *_args, **_kwargs):
            return EmptyResult()

    repository = ReadModelRepository(EmptySession())
    with pytest.raises(ApiError) as error:
        repository._resolve_execution("hidden-execution")
    assert error.value.status_code == 404
    assert error.value.code == "not_found"
    assert "无权访问" in error.value.message
