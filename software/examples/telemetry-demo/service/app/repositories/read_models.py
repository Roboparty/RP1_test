from __future__ import annotations

import base64
import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.errors import ApiError
from app.domain.mtbf import IMPLEMENTATION_KEY, calculate_mtbf
from app.schemas.read_models import (
    ArtifactView,
    AssetDetail,
    AssetList,
    AssetPerformanceTrends,
    AssetSummary,
    ConfigurationView,
    CurrentContextView,
    DashboardHome,
    DataSourceHealthView,
    DurationTotals,
    EventList,
    EventListItem,
    EvidenceSummary,
    ExecutionDetail,
    ExecutionJointAnalysis,
    ExecutionList,
    ExecutionListItem,
    ExecutionResultView,
    FreshnessView,
    HomeObjectGroup,
    HomeScope,
    HomeTestCase,
    JointAnalysisCycle,
    JointAnalysisMetric,
    JointAnalysisPoint,
    JointAnalysisSelection,
    JointAnalysisSeries,
    JointAnalysisStage,
    JointAnalysisSubject,
    JointAnalysisSummaryItem,
    MetricObservationView,
    MetricPeakView,
    MtbfConclusionItem,
    MtbfConclusionList,
    MtbfObservedSection,
    MtbfObservationView,
    MtbfStatisticalSection,
    MtbfVerifiedSection,
    PageMeta,
    PerformanceTrendMetric,
    PerformanceTrendPoint,
    PerformanceTrendSeries,
    SourceHealthItem,
    TestCaseDetail,
    TestCaseDurationItem,
    TestCaseDurationList,
)


def _number(value: Decimal | float | int | None) -> float:
    return float(value or 0)


def _encode_cursor(*parts: int) -> str:
    raw = ":".join(str(part) for part in parts).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_cursor(cursor: str | None, size: int = 1) -> tuple[int, ...]:
    if not cursor:
        return tuple(0 for _ in range(size))
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        parts = tuple(int(part) for part in base64.urlsafe_b64decode(padded).decode().split(":"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise ApiError(422, "invalid_cursor", "分页游标无效，请从第一页重新查询。") from exc
    if len(parts) != size or any(part < 0 for part in parts):
        raise ApiError(422, "invalid_cursor", "分页游标无效，请从第一页重新查询。")
    return parts


def _freshness(as_of_at: datetime, last_received_at: datetime | None) -> FreshnessView:
    if last_received_at is None:
        return FreshnessView(
            as_of_at=as_of_at,
            status="NO_DATA",
        )
    lag = max(0.0, (as_of_at - last_received_at).total_seconds())
    return FreshnessView(
        as_of_at=as_of_at,
        data_cutoff_at=last_received_at,
        last_received_at=last_received_at,
        lag_seconds=lag,
        status="FRESH" if lag <= 300 else "STALE",
    )


def _source_health_status(
    configured_status: str,
    last_receipt_status: str | None,
    last_error: str | None,
    lag_seconds: float | None,
) -> str:
    if configured_status == "DISABLED":
        return "DISABLED"
    if configured_status == "DEGRADED":
        return "DEGRADED"
    if last_receipt_status is None:
        return "NO_DATA"
    if last_receipt_status in {"REJECTED", "PARTIAL"} or last_error:
        return "ERROR"
    return "FRESH" if lag_seconds is not None and lag_seconds <= 300 else "STALE"


def _context_is_stale(
    as_of_at: datetime, status: str, last_received_at: datetime | None
) -> bool:
    if status != "RUNNING":
        return False
    return last_received_at is None or (as_of_at - last_received_at).total_seconds() > 300


def _is_source_warning(status: str) -> bool:
    return status not in {"FRESH", "DISABLED"}


def _live_elapsed_seconds(
    as_of_at: datetime,
    started_at: datetime | None,
    last_received_at: datetime | None,
    status: str,
) -> float:
    if status != "RUNNING" or started_at is None:
        return 0
    if last_received_at is None:
        cutoff = started_at
    elif (as_of_at - last_received_at).total_seconds() > 300:
        cutoff = last_received_at
    else:
        cutoff = as_of_at
    return max(0.0, (cutoff - started_at).total_seconds())


JOINT_METRIC_CODES = (
    "JOINT-TARGET-POSITION",
    "JOINT-ACTUAL-POSITION",
    "JOINT-TRACKING-ERROR",
    "JOINT-VELOCITY",
    "JOINT-TORQUE",
    "JOINT-CURRENT",
    "JOINT-TEMPERATURE",
    "JOINT-VIBRATION-RMS",
)
_JOINT_METRIC_ORDER = {
    metric_code: display_order
    for display_order, metric_code in enumerate(JOINT_METRIC_CODES)
}
_INVALID_POINT_QUALITIES = {"INVALID", "MISSING"}


@dataclass(frozen=True)
class JointStatisticPoint:
    sample_index: int
    value: float | None
    observed_at: datetime
    quality_status: str = "VALID"


@dataclass(frozen=True)
class JointStatisticSeries:
    metric_code: str
    unit: str
    quality_status: str
    started_at: datetime | None
    ended_at: datetime | None
    points: tuple[JointStatisticPoint, ...]


@dataclass(frozen=True)
class _JointSeriesMetadata:
    internal_id: int
    public_id: UUID
    subject_code: str
    subject_name: str
    subject_kind: str
    target_part_code: str | None
    subject_display_order: int
    subject_enabled: bool
    stage_public_id: UUID
    stage_code: str
    stage_name: str
    stage_sequence_no: int
    stage_status: str
    metric_code: str
    metric_name: str
    unit: str
    series_kind: str
    cycle_index: int
    point_count: int
    started_at: datetime | None
    ended_at: datetime | None
    sampling_interval_ms: int | None
    downsample_method: str | None
    quality_status: str


def _usable_points(
    points: Sequence[JointStatisticPoint],
) -> list[JointStatisticPoint]:
    return [
        point
        for point in points
        if point.value is not None
        and point.quality_status not in _INVALID_POINT_QUALITIES
    ]


def _summary_quality(
    points: Sequence[JointStatisticPoint],
    series_statuses: Sequence[str],
) -> str:
    usable = _usable_points(points)
    if not usable:
        return "NO_DATA"
    if (
        len(usable) != len(points)
        or any(point.quality_status != "VALID" for point in usable)
        or any(status != "VALID" for status in series_statuses)
    ):
        return "PARTIAL"
    return "VALID"


def _percentile(values: Sequence[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * percentile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def _rms(values: Sequence[float]) -> float | None:
    if not values:
        return None
    return math.sqrt(sum(value * value for value in values) / len(values))


def _summary_item(
    *,
    code: str,
    label: str,
    source_metric_code: str | None,
    value: float | None,
    unit: str,
    sample_count: int,
    quality_status: str,
) -> JointAnalysisSummaryItem:
    return JointAnalysisSummaryItem(
        code=code,
        label=label,
        source_metric_code=source_metric_code,
        value=value,
        unit=unit,
        sample_count=sample_count,
        quality_status=quality_status,
    )


def calculate_joint_summary(
    series: Sequence[JointStatisticSeries],
) -> list[JointAnalysisSummaryItem]:
    """Calculate the fixed joint-analysis summary without database dependencies."""
    by_metric: dict[str, list[JointStatisticSeries]] = {}
    for item in series:
        by_metric.setdefault(item.metric_code, []).append(item)

    def metric_points(metric_code: str) -> list[JointStatisticPoint]:
        points = [
            point
            for item in by_metric.get(metric_code, [])
            for point in item.points
        ]
        return sorted(points, key=lambda point: (point.observed_at, point.sample_index))

    def metric_statuses(metric_code: str) -> list[str]:
        return [item.quality_status for item in by_metric.get(metric_code, [])]

    def metric_unit(metric_code: str, fallback: str) -> str:
        metric_series = by_metric.get(metric_code, [])
        return metric_series[0].unit if metric_series else fallback

    summary: list[JointAnalysisSummaryItem] = []
    tracking_code = "JOINT-TRACKING-ERROR"
    tracking_points = metric_points(tracking_code)
    tracking_window_size = max(1, math.ceil(len(tracking_points) * 0.1)) if tracking_points else 0
    start_window = tracking_points[:tracking_window_size]
    end_window = tracking_points[-tracking_window_size:] if tracking_window_size else []
    start_values = [float(point.value) for point in _usable_points(start_window)]
    end_values = [float(point.value) for point in _usable_points(end_window)]
    all_tracking_values = [
        abs(float(point.value)) for point in _usable_points(tracking_points)
    ]
    start_rms = _rms(start_values)
    end_rms = _rms(end_values)
    tracking_unit = metric_unit(tracking_code, "")
    start_quality = _summary_quality(start_window, metric_statuses(tracking_code))
    end_quality = _summary_quality(end_window, metric_statuses(tracking_code))
    summary.extend(
        [
            _summary_item(
                code="TRACKING_ERROR_START_RMS",
                label="起始 10% 跟踪误差 RMS",
                source_metric_code=tracking_code,
                value=start_rms,
                unit=tracking_unit,
                sample_count=len(start_values),
                quality_status=start_quality,
            ),
            _summary_item(
                code="TRACKING_ERROR_END_RMS",
                label="结束 10% 跟踪误差 RMS",
                source_metric_code=tracking_code,
                value=end_rms,
                unit=tracking_unit,
                sample_count=len(end_values),
                quality_status=end_quality,
            ),
        ]
    )
    if start_rms is None or end_rms is None:
        change_rate = None
        change_quality = "NO_DATA"
    elif math.isclose(start_rms, 0.0, abs_tol=1e-15):
        change_rate = None
        change_quality = "NOT_CALCULABLE"
    else:
        change_rate = (end_rms - start_rms) / abs(start_rms) * 100
        change_quality = (
            "PARTIAL"
            if "PARTIAL" in {start_quality, end_quality}
            else "VALID"
        )
    summary.extend(
        [
            _summary_item(
                code="TRACKING_ERROR_CHANGE_RATE",
                label="跟踪误差 RMS 变化率",
                source_metric_code=tracking_code,
                value=change_rate,
                unit="%",
                sample_count=len(start_values) + len(end_values),
                quality_status=change_quality,
            ),
            _summary_item(
                code="TRACKING_ERROR_P95_ABS",
                label="P95 绝对跟踪误差",
                source_metric_code=tracking_code,
                value=_percentile(all_tracking_values, 0.95),
                unit=tracking_unit,
                sample_count=len(all_tracking_values),
                quality_status=_summary_quality(
                    tracking_points, metric_statuses(tracking_code)
                ),
            ),
        ]
    )

    torque_code = "JOINT-TORQUE"
    torque_points = metric_points(torque_code)
    torque_values = [
        abs(float(point.value)) for point in _usable_points(torque_points)
    ]
    torque_quality = _summary_quality(torque_points, metric_statuses(torque_code))
    torque_unit = metric_unit(torque_code, "")
    summary.extend(
        [
            _summary_item(
                code="TORQUE_MEAN_ABS",
                label="平均绝对力矩",
                source_metric_code=torque_code,
                value=(
                    sum(torque_values) / len(torque_values)
                    if torque_values
                    else None
                ),
                unit=torque_unit,
                sample_count=len(torque_values),
                quality_status=torque_quality,
            ),
            _summary_item(
                code="TORQUE_PEAK_ABS",
                label="峰值绝对力矩",
                source_metric_code=torque_code,
                value=max(torque_values) if torque_values else None,
                unit=torque_unit,
                sample_count=len(torque_values),
                quality_status=torque_quality,
            ),
        ]
    )

    current_code = "JOINT-CURRENT"
    current_points = metric_points(current_code)
    current_values = [
        float(point.value) for point in _usable_points(current_points)
    ]
    current_quality = _summary_quality(current_points, metric_statuses(current_code))
    current_unit = metric_unit(current_code, "")
    summary.extend(
        [
            _summary_item(
                code="CURRENT_MEAN",
                label="平均电流",
                source_metric_code=current_code,
                value=(
                    sum(current_values) / len(current_values)
                    if current_values
                    else None
                ),
                unit=current_unit,
                sample_count=len(current_values),
                quality_status=current_quality,
            ),
            _summary_item(
                code="CURRENT_PEAK",
                label="峰值电流",
                source_metric_code=current_code,
                value=max(current_values) if current_values else None,
                unit=current_unit,
                sample_count=len(current_values),
                quality_status=current_quality,
            ),
        ]
    )

    temperature_code = "JOINT-TEMPERATURE"
    temperature_points = metric_points(temperature_code)
    usable_temperatures = _usable_points(temperature_points)
    initial_temperature = (
        float(usable_temperatures[0].value) if usable_temperatures else None
    )
    final_temperature = (
        float(usable_temperatures[-1].value) if usable_temperatures else None
    )
    temperature_quality = _summary_quality(
        temperature_points, metric_statuses(temperature_code)
    )
    temperature_unit = metric_unit(temperature_code, "")
    summary.extend(
        [
            _summary_item(
                code="TEMPERATURE_INITIAL",
                label="初始温度",
                source_metric_code=temperature_code,
                value=initial_temperature,
                unit=temperature_unit,
                sample_count=1 if initial_temperature is not None else 0,
                quality_status=temperature_quality,
            ),
            _summary_item(
                code="TEMPERATURE_FINAL",
                label="结束温度",
                source_metric_code=temperature_code,
                value=final_temperature,
                unit=temperature_unit,
                sample_count=1 if final_temperature is not None else 0,
                quality_status=temperature_quality,
            ),
            _summary_item(
                code="TEMPERATURE_RISE",
                label="温升",
                source_metric_code=temperature_code,
                value=(
                    final_temperature - initial_temperature
                    if initial_temperature is not None
                    and final_temperature is not None
                    else None
                ),
                unit=temperature_unit,
                sample_count=len(usable_temperatures),
                quality_status=temperature_quality,
            ),
        ]
    )

    vibration_code = "JOINT-VIBRATION-RMS"
    vibration_points = metric_points(vibration_code)
    vibration_values = [
        float(point.value) for point in _usable_points(vibration_points)
    ]
    summary.append(
        _summary_item(
            code="VIBRATION_MAX",
            label="最大振动 RMS",
            source_metric_code=vibration_code,
            value=max(vibration_values) if vibration_values else None,
            unit=metric_unit(vibration_code, ""),
            sample_count=len(vibration_values),
            quality_status=_summary_quality(
                vibration_points, metric_statuses(vibration_code)
            ),
        )
    )

    starts: list[datetime] = []
    ends: list[datetime] = []
    duration_is_partial = False
    for item in series:
        observed_times = [point.observed_at for point in item.points]
        start = item.started_at or (min(observed_times) if observed_times else None)
        end = item.ended_at or (max(observed_times) if observed_times else None)
        if start is not None:
            starts.append(start)
        if end is not None:
            ends.append(end)
        if (
            item.started_at is None
            or item.ended_at is None
            or item.quality_status != "VALID"
        ):
            duration_is_partial = True
    if starts and ends:
        cycle_duration = max(0.0, (max(ends) - min(starts)).total_seconds())
        duration_quality = "PARTIAL" if duration_is_partial else "VALID"
    else:
        cycle_duration = None
        duration_quality = "NO_DATA"
    summary.append(
        _summary_item(
            code="CYCLE_DURATION",
            label="周期时长",
            source_metric_code=None,
            value=cycle_duration,
            unit="s",
            sample_count=len(series),
            quality_status=duration_quality,
        )
    )
    return summary


def _downsample_points(
    points: Sequence[JointAnalysisPoint], limit: int = 120
) -> list[JointAnalysisPoint]:
    if len(points) <= limit:
        return list(points)
    if limit < 2:
        return [points[0]] if limit == 1 else []
    indices = [
        round(position * (len(points) - 1) / (limit - 1))
        for position in range(limit)
    ]
    return [points[index] for index in indices]


def _invalid_joint_selection(field: str, value: object) -> ApiError:
    return ApiError(
        422,
        "invalid_joint_analysis_selection",
        "关节分析筛选值在当前 Execution 的可用数据中不存在。",
        details=[
            {
                "field": field,
                "message": f"不可用的筛选值：{value}",
                "code": "invalid_selection",
            }
        ],
    )


def _resolve_joint_selection(
    metadata: Sequence[_JointSeriesMetadata],
    *,
    subject_code: str | None,
    stage_code: str | None,
    cycle_index: int | None,
) -> JointAnalysisSelection:
    if not metadata:
        if subject_code is not None:
            raise _invalid_joint_selection("subject_code", subject_code)
        if stage_code is not None:
            raise _invalid_joint_selection("stage_code", stage_code)
        if cycle_index is not None:
            raise _invalid_joint_selection("cycle_index", cycle_index)
        return JointAnalysisSelection()

    subject_rows = sorted(
        metadata,
        key=lambda item: (
            item.subject_display_order,
            item.subject_code,
        ),
    )
    available_subjects = {item.subject_code for item in subject_rows}
    resolved_subject = subject_code or subject_rows[0].subject_code
    if resolved_subject not in available_subjects:
        raise _invalid_joint_selection("subject_code", resolved_subject)

    subject_metadata = [
        item for item in metadata if item.subject_code == resolved_subject
    ]
    available_stages = {item.stage_code for item in subject_metadata}
    if stage_code is not None and stage_code not in available_stages:
        raise _invalid_joint_selection("stage_code", stage_code)
    if stage_code is None:
        stage_rows = sorted(
            subject_metadata,
            key=lambda item: (
                item.stage_code != "STEADY_RUN",
                item.stage_sequence_no,
                item.stage_code,
            ),
        )
        resolved_stage = stage_rows[0].stage_code
    else:
        resolved_stage = stage_code

    stage_metadata = [
        item
        for item in subject_metadata
        if item.stage_code == resolved_stage
    ]
    available_cycles = {item.cycle_index for item in stage_metadata}
    if cycle_index is not None and cycle_index not in available_cycles:
        raise _invalid_joint_selection("cycle_index", cycle_index)
    resolved_cycle = cycle_index if cycle_index is not None else max(available_cycles)
    return JointAnalysisSelection(
        subject_code=resolved_subject,
        stage_code=resolved_stage,
        cycle_index=resolved_cycle,
    )


LEDGER_CTE = """
overall_scope AS (
    SELECT DISTINCT ON (asset_kind) id, asset_kind
    FROM reliability.mtbf_scope
    WHERE scope_code = 'OVERALL' AND status = 'PUBLISHED'
    ORDER BY asset_kind, version DESC
),
ledger AS (
    SELECT ri.id, ri.asset_id, ri.execution_id, ri.active_seconds, ri.received_at,
           CASE
             WHEN ea.assignment_status = 'PENDING_CONFIG_REVIEW'
               OR esa.inclusion_status = 'PENDING' THEN 'PENDING'
             WHEN ea.eligible
               AND ea.assignment_status = 'CONFIRMED'
               AND ea.quality_status IN ('VALID', 'PARTIAL')
               AND esa.inclusion_status = 'INCLUDED'
               AND ri.source_kind <> 'LEGACY_REPORTED' THEN 'INCLUDED'
             ELSE 'EXCLUDED'
           END AS disposition
    FROM test.runtime_interval ri
    JOIN test.asset a ON a.id = ri.asset_id
    LEFT JOIN overall_scope os ON os.asset_kind = a.asset_kind
    LEFT JOIN reliability.exposure_assessment ea
      ON ea.runtime_interval_id = ri.id
    LEFT JOIN reliability.exposure_scope_assignment esa
      ON esa.assessment_id = ea.id AND esa.scope_id = os.id
    WHERE NOT ri.voided
)
"""


class ReadModelRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def _as_of(self) -> datetime:
        return self.session.execute(text("SELECT clock_timestamp()")).scalar_one()

    def _global_freshness(self, as_of_at: datetime | None = None) -> FreshnessView:
        as_of_at = as_of_at or self._as_of()
        last_received = self.session.execute(
            text(
                """
                SELECT max(received_at)
                FROM (
                    SELECT received_at FROM test.test_execution
                    UNION ALL SELECT received_at FROM test.test_event
                    UNION ALL SELECT received_at FROM test.runtime_interval
                    UNION ALL SELECT received_at FROM integration.ingestion_receipt
                ) facts
                """
            )
        ).scalar_one_or_none()
        return _freshness(as_of_at, last_received)

    def data_sources_health(self, as_of_at: datetime | None = None) -> DataSourceHealthView:
        as_of_at = as_of_at or self._as_of()
        rows = self.session.execute(
            text(
                """
                SELECT s.public_id, s.source_code, s.source_type, s.status,
                       latest.received_at, latest.receipt_status, latest.error_detail,
                       successful.last_success_at
                FROM integration.ingestion_source s
                LEFT JOIN LATERAL (
                    SELECT r.received_at, r.status AS receipt_status,
                           nullif(r.error_detail, '') AS error_detail
                    FROM integration.ingestion_receipt r
                    WHERE r.source_id = s.id
                    ORDER BY r.received_at DESC, r.id DESC
                    LIMIT 1
                ) latest ON true
                LEFT JOIN LATERAL (
                    SELECT max(r.received_at) AS last_success_at
                    FROM integration.ingestion_receipt r
                    WHERE r.source_id = s.id
                      AND r.status IN ('ACCEPTED', 'DUPLICATE')
                ) successful ON true
                ORDER BY s.source_code
                """
            )
        ).mappings()
        sources: list[SourceHealthItem] = []
        for row in rows:
            received_at = row["received_at"]
            lag = None if received_at is None else max(0.0, (as_of_at - received_at).total_seconds())
            status = _source_health_status(
                row["status"], row["receipt_status"], row["error_detail"], lag
            )
            sources.append(
                SourceHealthItem(
                    id=row["public_id"],
                    code=row["source_code"],
                    source_type=row["source_type"],
                    configured_status=row["status"],
                    last_received_at=received_at,
                    last_receipt_status=row["receipt_status"],
                    last_success_at=row["last_success_at"],
                    lag_seconds=lag,
                    freshness_status=status,
                    last_error=row["error_detail"],
                )
            )
        return DataSourceHealthView(
            as_of_at=as_of_at,
            data_cutoff_at=max(
                (item.last_received_at for item in sources if item.last_received_at),
                default=None,
            ),
            sources=sources,
            healthy_count=sum(item.freshness_status == "FRESH" for item in sources),
            warning_count=sum(_is_source_warning(item.freshness_status) for item in sources),
        )

    def dashboard_home(self) -> DashboardHome:
        as_of_at = self._as_of()
        freshness = self._global_freshness(as_of_at)
        source_health = self.data_sources_health(as_of_at)
        anomaly_row = self.session.execute(
            text(
                """
                SELECT
                  count(*) FILTER (
                    WHERE i.classification = 'BLOCKED' AND i.review_status = 'PENDING'
                      AND NOT i.voided
                  ) AS blocked_count,
                  (SELECT count(*) FROM test.test_event e
                   WHERE NOT e.voided AND e.data_quality IN ('INVALID', 'MISSING')) AS quality_count
                FROM reliability.interruption i
                """
            )
        ).mappings().one()
        anomalies: list[str] = []
        if anomaly_row["blocked_count"]:
            anomalies.append(f"{anomaly_row['blocked_count']} 个 Blocked 中断待复核")
        if anomaly_row["quality_count"]:
            anomalies.append(f"{anomaly_row['quality_count']} 条事件存在无效或缺失数据")

        scope_rows = list(
            self.session.execute(
                text(
                    """
                    WITH scoped_assets AS (
                      SELECT a.id, a.asset_kind,
                             CASE
                               WHEN a.asset_kind = 'WHOLE_MACHINE' THEN 'SYS'
                               ELSE mp.target_part_code
                             END AS target_part_code
                      FROM test.asset a
                      LEFT JOIN test.module_profile mp
                        ON mp.asset_id = a.id AND a.asset_kind = 'MODULE'
                      WHERE NOT a.voided
                    )
                    SELECT scoped.asset_kind,
                           target_part.part_code AS target_part_code,
                           target_part.name AS target_part_name,
                           count(DISTINCT scoped.id) AS asset_count,
                           count(DISTINCT e.id) FILTER (
                             WHERE e.status = 'RUNNING'
                           ) AS active_execution_count,
                           count(DISTINCT scoped.id) FILTER (
                             WHERE latest_health.health_status = 'ABNORMAL'
                           ) AS abnormal_asset_count,
                           CASE
                             WHEN count(*) FILTER (WHERE e.status = 'RUNNING') > 0 THEN 'RUNNING'
                             WHEN count(*) FILTER (WHERE e.status = 'PAUSED') > 0 THEN 'PAUSED'
                             WHEN count(*) FILTER (WHERE e.status = 'BLOCKED') > 0 THEN 'BLOCKED'
                             WHEN count(*) FILTER (WHERE e.status = 'FAILED') > 0 THEN 'FAILED'
                             WHEN count(*) FILTER (WHERE e.status = 'COMPLETED') > 0 THEN 'COMPLETED'
                             ELSE 'NOT_STARTED'
                           END AS status
                    FROM scoped_assets scoped
                    JOIN catalog.test_target_part target_part
                      ON target_part.part_code = scoped.target_part_code
                    LEFT JOIN test.test_execution e
                      ON e.asset_id = scoped.id AND e.status <> 'VOIDED'
                    LEFT JOIN LATERAL (
                      SELECT health_status
                      FROM health.health_check_run health
                      WHERE health.asset_id = scoped.id
                      ORDER BY coalesce(
                        health.completed_at, health.started_at, health.scheduled_at
                      ) DESC NULLS LAST, health.id DESC
                      LIMIT 1
                    ) latest_health ON true
                    GROUP BY scoped.asset_kind, target_part.part_code, target_part.name
                    ORDER BY scoped.asset_kind, target_part.part_code
                    """
                )
            ).mappings()
        )
        scope_keys = {
            (row["asset_kind"], row["target_part_code"]) for row in scope_rows
        }
        part_codes = sorted({part_code for _, part_code in scope_keys})
        case_map: dict[tuple[str, str], list[HomeTestCase]] = {
            key: [] for key in scope_keys
        }
        if scope_keys:
            case_rows = self.session.execute(
                text(
                    f"""
                    WITH {LEDGER_CTE},
                    execution_ledger AS (
                      SELECT execution_id,
                        sum(active_seconds) AS total_seconds,
                        sum(active_seconds) FILTER (WHERE disposition = 'INCLUDED') AS effective_seconds,
                        sum(active_seconds) FILTER (WHERE disposition = 'EXCLUDED') AS excluded_seconds,
                        sum(active_seconds) FILTER (WHERE disposition = 'PENDING') AS pending_seconds
                      FROM ledger WHERE execution_id IS NOT NULL GROUP BY execution_id
                    ),
                    scoped_assets AS (
                      SELECT a.id, a.asset_kind,
                             CASE
                               WHEN a.asset_kind = 'WHOLE_MACHINE' THEN 'SYS'
                               ELSE mp.target_part_code
                             END AS target_part_code
                      FROM test.asset a
                      LEFT JOIN test.module_profile mp
                        ON mp.asset_id = a.id AND a.asset_kind = 'MODULE'
                      WHERE NOT a.voided
                        AND CASE
                              WHEN a.asset_kind = 'WHOLE_MACHINE' THEN 'SYS'
                              ELSE mp.target_part_code
                            END = ANY(CAST(:part_codes AS text[]))
                    )
                    SELECT scoped_assets.asset_kind, tc.public_id, tc.case_code, tc.name,
                           tc.target_part_code, target_part.name AS target_part_name,
                           CASE
                             WHEN count(*) FILTER (WHERE e.status = 'RUNNING') > 0 THEN 'RUNNING'
                             WHEN count(*) FILTER (WHERE e.status = 'PAUSED') > 0 THEN 'PAUSED'
                             WHEN count(*) FILTER (WHERE e.status = 'BLOCKED') > 0 THEN 'BLOCKED'
                             WHEN count(*) FILTER (WHERE e.status = 'FAILED') > 0 THEN 'FAILED'
                             WHEN count(*) FILTER (WHERE e.status = 'COMPLETED') > 0 THEN 'COMPLETED'
                             WHEN count(e.id) = 0 THEN 'NOT_STARTED'
                             ELSE max(e.status)
                           END AS status,
                           count(e.id) AS execution_count,
                           count(DISTINCT scoped_assets.id) AS scope_asset_count,
                           coalesce(sum(el.total_seconds), 0) AS total_seconds,
                           coalesce(sum(el.effective_seconds), 0) AS effective_seconds,
                           coalesce(sum(el.excluded_seconds), 0) AS excluded_seconds,
                           coalesce(sum(el.pending_seconds), 0) AS pending_seconds,
                           coalesce(sum(
                             CASE WHEN e.status = 'RUNNING' AND e.normalized_started_at IS NOT NULL
                               THEN greatest(0, extract(epoch FROM (
                                 CASE
                                   WHEN e.received_at IS NULL THEN e.normalized_started_at
                                   WHEN e.received_at < :as_of_at - interval '5 minutes' THEN e.received_at
                                   ELSE :as_of_at
                                 END - e.normalized_started_at
                               )))
                               ELSE 0 END
                           ), 0) AS current_elapsed_seconds,
                           max(CASE
                             WHEN jsonb_typeof(published.procedure_spec -> 'target_duration_seconds') = 'number'
                             THEN (published.procedure_spec ->> 'target_duration_seconds')::numeric
                             ELSE NULL END) AS target_seconds
                    FROM catalog.test_case tc
                    JOIN scoped_assets
                      ON scoped_assets.target_part_code = tc.target_part_code
                     AND scoped_assets.asset_kind = tc.asset_kind
                    JOIN catalog.test_target_part target_part
                      ON target_part.part_code = tc.target_part_code
                    JOIN LATERAL (
                      SELECT tcv.id, tcv.procedure_spec
                      FROM catalog.test_case_version tcv
                      WHERE tcv.test_case_id = tc.id AND tcv.status = 'PUBLISHED'
                      ORDER BY tcv.published_at DESC NULLS LAST, tcv.id DESC
                      LIMIT 1
                    ) published ON true
                    LEFT JOIN test.test_execution e
                      ON e.asset_id = scoped_assets.id
                     AND e.status <> 'VOIDED'
                     AND EXISTS (
                       SELECT 1 FROM catalog.test_case_version execution_version
                       WHERE execution_version.id = e.test_case_version_id
                         AND execution_version.test_case_id = tc.id
                     )
                    LEFT JOIN execution_ledger el ON el.execution_id = e.id
                    WHERE tc.enabled
                      AND tc.target_part_code = ANY(CAST(:part_codes AS text[]))
                    GROUP BY scoped_assets.asset_kind, tc.id, tc.public_id, tc.case_code, tc.name,
                             tc.target_part_code, target_part.name
                    ORDER BY scoped_assets.asset_kind, tc.target_part_code,
                      (count(*) FILTER (WHERE e.status = 'RUNNING') > 0) DESC,
                      tc.case_code
                    """
                ),
                {"part_codes": part_codes, "as_of_at": as_of_at},
            ).mappings()
            for row in case_rows:
                target = _number(row["target_seconds"]) if row["target_seconds"] is not None else None
                effective = _number(row["effective_seconds"])
                case_map[(row["asset_kind"], row["target_part_code"])].append(
                    HomeTestCase(
                        id=row["public_id"],
                        code=row["case_code"],
                        name=row["name"],
                        target_part_code=row["target_part_code"],
                        target_part_name=row["target_part_name"],
                        status=row["status"],
                        current_elapsed_seconds=_number(row["current_elapsed_seconds"]),
                        cumulative_duration_seconds=_number(row["total_seconds"]),
                        effective_exposure_seconds=effective,
                        excluded_seconds=_number(row["excluded_seconds"]),
                        pending_seconds=_number(row["pending_seconds"]),
                        execution_count=int(row["execution_count"]),
                        scope_asset_count=int(row["scope_asset_count"]),
                        target_duration_seconds=target,
                        target_progress_percent=None if not target else effective / target * 100,
                    )
                )

        part_duration_map = {
            key: DurationTotals(
                total_duration_seconds=sum(item.cumulative_duration_seconds for item in items),
                effective_exposure_seconds=sum(item.effective_exposure_seconds for item in items),
                excluded_seconds=sum(item.excluded_seconds for item in items),
                pending_seconds=sum(item.pending_seconds for item in items),
                active_elapsed_seconds=sum(item.current_elapsed_seconds for item in items),
            )
            for key, items in case_map.items()
        }

        groups: list[HomeObjectGroup] = []
        for kind in ("WHOLE_MACHINE", "MODULE"):
            objects = [
                HomeScope(
                    id=f"{row['asset_kind']}:{row['target_part_code']}",
                    code=row["target_part_code"],
                    name=row["target_part_name"],
                    asset_kind=row["asset_kind"],
                    target_part_code=row["target_part_code"],
                    target_part_name=row["target_part_name"],
                    status=row["status"],
                    part_durations=part_duration_map[
                        (row["asset_kind"], row["target_part_code"])
                    ],
                    part_asset_count=int(row["asset_count"]),
                    active_execution_count=int(row["active_execution_count"]),
                    abnormal_asset_count=int(row["abnormal_asset_count"]),
                    test_cases=case_map[(row["asset_kind"], row["target_part_code"])],
                )
                for row in scope_rows
                if row["asset_kind"] == kind
            ]
            groups.append(HomeObjectGroup(asset_kind=kind, objects=objects))
        return DashboardHome(
            freshness=freshness,
            data_sources=source_health,
            anomaly_count=sum(int(value or 0) for value in anomaly_row.values()),
            anomalies=anomalies,
            groups=groups,
        )

    def test_case_durations(
        self,
        *,
        asset_kind: str | None,
        asset: str | None,
        test_case: str | None,
        status: str | None,
        search: str | None,
        limit: int,
        cursor: str | None,
    ) -> TestCaseDurationList:
        as_of_at = self._as_of()
        cursor_case, cursor_asset = _decode_cursor(cursor, 2)
        pattern = f"%{search.strip()}%" if search and search.strip() else None
        rows = list(
            self.session.execute(
                text(
                    f"""
                    WITH {LEDGER_CTE},
                    execution_ledger AS (
                      SELECT execution_id,
                        sum(active_seconds) AS total_seconds,
                        sum(active_seconds) FILTER (WHERE disposition = 'INCLUDED') AS effective_seconds,
                        sum(active_seconds) FILTER (WHERE disposition = 'EXCLUDED') AS excluded_seconds,
                        sum(active_seconds) FILTER (WHERE disposition = 'PENDING') AS pending_seconds,
                        max(received_at) AS last_fact_received_at
                      FROM ledger WHERE execution_id IS NOT NULL GROUP BY execution_id
                    ),
                    grouped AS (
                      SELECT tc.id AS case_internal_id, a.id AS asset_internal_id,
                        tc.public_id, tc.case_code, tc.name, tc.asset_kind,
                        a.public_id AS asset_public_id, a.asset_code,
                        coalesce(nullif(a.display_name, ''), a.asset_code) AS asset_name,
                        CASE
                          WHEN count(*) FILTER (WHERE e.status = 'RUNNING') > 0 THEN 'RUNNING'
                          WHEN count(*) FILTER (WHERE e.status = 'BLOCKED') > 0 THEN 'BLOCKED'
                          WHEN count(*) FILTER (WHERE e.status = 'FAILED') > 0 THEN 'FAILED'
                          WHEN count(*) FILTER (WHERE e.status = 'COMPLETED') > 0 THEN 'COMPLETED'
                          ELSE max(e.status)
                        END AS status,
                        count(*) AS execution_count,
                        greatest(
                          max(e.received_at), max(el.last_fact_received_at)
                        ) AS data_cutoff_at,
                        coalesce(sum(el.total_seconds), 0) AS total_seconds,
                        coalesce(sum(el.effective_seconds), 0) AS effective_seconds,
                        coalesce(sum(el.excluded_seconds), 0) AS excluded_seconds,
                        coalesce(sum(el.pending_seconds), 0) AS pending_seconds,
                        coalesce(sum(CASE
                          WHEN e.status = 'RUNNING' AND e.normalized_started_at IS NOT NULL
                          THEN greatest(0, extract(epoch FROM (
                            CASE
                              WHEN e.received_at IS NULL THEN e.normalized_started_at
                              WHEN e.received_at < :as_of_at - interval '5 minutes' THEN e.received_at
                              ELSE :as_of_at
                            END - e.normalized_started_at
                          )))
                          ELSE 0 END), 0) AS active_elapsed_seconds
                      FROM test.test_execution e
                      JOIN test.asset a ON a.id = e.asset_id
                      JOIN catalog.test_case_version tcv ON tcv.id = e.test_case_version_id
                      JOIN catalog.test_case tc ON tc.id = tcv.test_case_id
                      LEFT JOIN execution_ledger el ON el.execution_id = e.id
                      WHERE e.status <> 'VOIDED' AND NOT a.voided
                        AND (CAST(:asset_kind AS text) IS NULL OR tc.asset_kind = CAST(:asset_kind AS text))
                        AND (CAST(:asset AS text) IS NULL
                          OR a.public_id::text = :asset OR a.asset_code = :asset)
                        AND (CAST(:test_case AS text) IS NULL
                          OR tc.public_id::text = :test_case OR tc.case_code = :test_case)
                        AND (CAST(:pattern AS text) IS NULL
                          OR tc.case_code ILIKE :pattern OR tc.name ILIKE :pattern
                          OR a.asset_code ILIKE :pattern OR a.display_name ILIKE :pattern)
                      GROUP BY tc.id, tc.public_id, tc.case_code, tc.name, tc.asset_kind,
                               a.id, a.public_id, a.asset_code, a.display_name
                    ),
                    filtered AS (
                      SELECT * FROM grouped
                      WHERE (CAST(:status AS text) IS NULL OR status = CAST(:status AS text))
                    ),
                    with_totals AS (
                      SELECT *,
                      sum(total_seconds) OVER () AS all_total_seconds,
                      sum(effective_seconds) OVER () AS all_effective_seconds,
                      sum(excluded_seconds) OVER () AS all_excluded_seconds,
                      sum(pending_seconds) OVER () AS all_pending_seconds,
                      sum(active_elapsed_seconds) OVER () AS all_active_elapsed_seconds,
                      max(data_cutoff_at) OVER () AS all_data_cutoff_at
                      FROM filtered
                    )
                    SELECT * FROM with_totals
                    WHERE (case_internal_id, asset_internal_id) > (:cursor_case, :cursor_asset)
                    ORDER BY case_internal_id, asset_internal_id
                    LIMIT :fetch_limit
                    """
                ),
                {
                    "as_of_at": as_of_at,
                    "asset_kind": asset_kind,
                    "asset": asset,
                    "test_case": test_case,
                    "status": status,
                    "pattern": pattern,
                    "cursor_case": cursor_case,
                    "cursor_asset": cursor_asset,
                    "fetch_limit": limit + 1,
                },
            ).mappings()
        )
        has_more = len(rows) > limit
        page_rows = rows[:limit]
        items = [
            TestCaseDurationItem(
                id=row["public_id"],
                code=row["case_code"],
                name=row["name"],
                asset_kind=row["asset_kind"],
                asset_id=row["asset_public_id"],
                asset_code=row["asset_code"],
                asset_name=row["asset_name"],
                status=row["status"],
                execution_count=int(row["execution_count"]),
                durations=DurationTotals(
                    total_duration_seconds=_number(row["total_seconds"]),
                    effective_exposure_seconds=_number(row["effective_seconds"]),
                    excluded_seconds=_number(row["excluded_seconds"]),
                    pending_seconds=_number(row["pending_seconds"]),
                    active_elapsed_seconds=_number(row["active_elapsed_seconds"]),
                ),
                data_cutoff_at=row["data_cutoff_at"],
            )
            for row in page_rows
        ]
        first = page_rows[0] if page_rows else None
        last = page_rows[-1] if page_rows else None
        totals = DurationTotals(
            total_duration_seconds=_number(first["all_total_seconds"]) if first else 0,
            effective_exposure_seconds=_number(first["all_effective_seconds"]) if first else 0,
            excluded_seconds=_number(first["all_excluded_seconds"]) if first else 0,
            pending_seconds=_number(first["all_pending_seconds"]) if first else 0,
            active_elapsed_seconds=_number(first["all_active_elapsed_seconds"]) if first else 0,
        )
        return TestCaseDurationList(
            items=items,
            page=PageMeta(
                limit=limit,
                next_cursor=(
                    _encode_cursor(last["case_internal_id"], last["asset_internal_id"])
                    if has_more and last
                    else None
                ),
            ),
            totals=totals,
            as_of_at=as_of_at,
            data_cutoff_at=first["all_data_cutoff_at"] if first else None,
        )

    def list_assets(
        self,
        *,
        asset_kind: str | None,
        target_part_code: str | None,
        status: str | None,
        search: str | None,
        limit: int,
        cursor: str | None,
    ) -> AssetList:
        as_of_at = self._as_of()
        (after_id,) = _decode_cursor(cursor)
        pattern = f"%{search.strip()}%" if search and search.strip() else None
        rows = list(
            self.session.execute(
                text(
                    f"""
                    WITH {LEDGER_CTE},
                    durations AS (
                      SELECT asset_id,
                        sum(active_seconds) AS total_seconds,
                        sum(active_seconds) FILTER (WHERE disposition = 'INCLUDED') AS effective_seconds,
                        sum(active_seconds) FILTER (WHERE disposition = 'EXCLUDED') AS excluded_seconds,
                        sum(active_seconds) FILTER (WHERE disposition = 'PENDING') AS pending_seconds
                      FROM ledger GROUP BY asset_id
                    ),
                    last_data AS (
                      SELECT asset_id, max(received_at) AS last_received_at
                      FROM (
                        SELECT asset_id, received_at FROM test.test_execution
                        UNION ALL SELECT asset_id, received_at FROM test.test_event
                        UNION ALL SELECT asset_id, received_at FROM test.runtime_interval
                      ) facts GROUP BY asset_id
                    )
                    SELECT a.id AS internal_id, a.public_id, a.asset_code, a.asset_kind,
                           coalesce(nullif(a.display_name, ''), a.asset_code) AS display_name,
                           target_part.part_code AS target_part_code,
                           target_part.name AS target_part_name,
                           a.product_family, b.batch_code, a.serial_number, a.lifecycle_status,
                           current_execution.public_id AS execution_public_id,
                           current_execution.execution_code, current_execution.case_code,
                           current_execution.current_elapsed_seconds,
                           latest_health.health_status,
                           coalesce(d.total_seconds, 0) AS total_seconds,
                           coalesce(d.effective_seconds, 0) AS effective_seconds,
                           coalesce(d.excluded_seconds, 0) AS excluded_seconds,
                           coalesce(d.pending_seconds, 0) AS pending_seconds,
                           ld.last_received_at,
                           max(ld.last_received_at) OVER () AS all_data_cutoff_at
                    FROM test.asset a
                    LEFT JOIN test.manufacturing_batch b ON b.id = a.batch_id
                    LEFT JOIN test.module_profile mp
                      ON mp.asset_id = a.id AND a.asset_kind = 'MODULE'
                    JOIN catalog.test_target_part target_part
                      ON target_part.part_code = CASE
                        WHEN a.asset_kind = 'WHOLE_MACHINE' THEN 'SYS'
                        ELSE mp.target_part_code
                      END
                    LEFT JOIN durations d ON d.asset_id = a.id
                    LEFT JOIN last_data ld ON ld.asset_id = a.id
                    LEFT JOIN LATERAL (
                      SELECT e.public_id, e.execution_code, tc.case_code,
                        CASE WHEN e.status = 'RUNNING' AND e.normalized_started_at IS NOT NULL
                          THEN greatest(0, extract(epoch FROM (
                            CASE
                              WHEN e.received_at IS NULL THEN e.normalized_started_at
                              WHEN e.received_at < :as_of_at - interval '5 minutes' THEN e.received_at
                              ELSE :as_of_at
                            END - e.normalized_started_at
                          )))
                          ELSE 0 END AS current_elapsed_seconds
                      FROM test.test_execution e
                      JOIN catalog.test_case_version tcv ON tcv.id = e.test_case_version_id
                      JOIN catalog.test_case tc ON tc.id = tcv.test_case_id
                      WHERE e.asset_id = a.id AND e.status IN ('RUNNING', 'PAUSED', 'BLOCKED')
                      ORDER BY (e.status = 'RUNNING') DESC,
                               e.normalized_started_at DESC NULLS LAST, e.id DESC
                      LIMIT 1
                    ) current_execution ON true
                    LEFT JOIN LATERAL (
                      SELECT h.health_status FROM health.health_check_run h
                      WHERE h.asset_id = a.id
                      ORDER BY coalesce(h.completed_at, h.started_at, h.scheduled_at) DESC NULLS LAST,
                               h.id DESC LIMIT 1
                    ) latest_health ON true
                    WHERE a.id > :after_id AND NOT a.voided
                      AND (CAST(:asset_kind AS text) IS NULL OR a.asset_kind = CAST(:asset_kind AS text))
                      AND (
                        CAST(:target_part_code AS text) IS NULL
                        OR target_part.part_code = CAST(:target_part_code AS text)
                      )
                      AND (CAST(:status AS text) IS NULL OR a.lifecycle_status = CAST(:status AS text))
                      AND (CAST(:pattern AS text) IS NULL OR a.asset_code ILIKE :pattern
                           OR a.display_name ILIKE :pattern OR a.serial_number ILIKE :pattern)
                    ORDER BY a.id
                    LIMIT :fetch_limit
                    """
                ),
                {
                    "as_of_at": as_of_at,
                    "after_id": after_id,
                    "asset_kind": asset_kind,
                    "target_part_code": target_part_code,
                    "status": status,
                    "pattern": pattern,
                    "fetch_limit": limit + 1,
                },
            ).mappings()
        )
        has_more = len(rows) > limit
        rows = rows[:limit]
        items = [self._asset_summary(row, as_of_at) for row in rows]
        return AssetList(
            items=items,
            page=PageMeta(
                limit=limit,
                next_cursor=_encode_cursor(rows[-1]["internal_id"]) if has_more and rows else None,
            ),
            as_of_at=as_of_at,
            data_cutoff_at=rows[0]["all_data_cutoff_at"] if rows else None,
        )

    @staticmethod
    def _asset_summary(row: dict, as_of_at: datetime) -> AssetSummary:
        freshness = _freshness(as_of_at, row["last_received_at"])
        return AssetSummary(
            id=row["public_id"],
            code=row["asset_code"],
            name=row["display_name"],
            asset_kind=row["asset_kind"],
            target_part_code=row.get("target_part_code"),
            target_part_name=row.get("target_part_name"),
            product_family=row["product_family"],
            batch_code=row["batch_code"],
            serial_number=row["serial_number"],
            lifecycle_status=row["lifecycle_status"],
            current_test_case_code=row["case_code"],
            current_execution_id=row["execution_public_id"],
            current_execution_code=row["execution_code"],
            health_status=row["health_status"],
            durations=DurationTotals(
                total_duration_seconds=_number(row["total_seconds"]),
                effective_exposure_seconds=_number(row["effective_seconds"]),
                excluded_seconds=_number(row["excluded_seconds"]),
                pending_seconds=_number(row["pending_seconds"]),
                active_elapsed_seconds=_number(row["current_elapsed_seconds"]),
            ),
            freshness=freshness,
        )

    def _resolve_asset(self, identifier: str) -> dict:
        row = self.session.execute(
            text(
                """
                SELECT id, public_id, asset_code
                FROM test.asset
                WHERE NOT voided AND (public_id::text = :identifier OR asset_code = :identifier)
                """
            ),
            {"identifier": identifier},
        ).mappings().first()
        if not row:
            raise ApiError(404, "not_found", "未找到样品，或当前用户无权访问该样品。")
        return dict(row)

    def get_asset(self, identifier: str) -> AssetDetail:
        as_of_at = self._as_of()
        resolved = self._resolve_asset(identifier)
        row = self.session.execute(
            text(
                f"""
                WITH {LEDGER_CTE},
                durations AS (
                  SELECT asset_id,
                    sum(active_seconds) AS total_seconds,
                    sum(active_seconds) FILTER (WHERE disposition = 'INCLUDED') AS effective_seconds,
                    sum(active_seconds) FILTER (WHERE disposition = 'EXCLUDED') AS excluded_seconds,
                    sum(active_seconds) FILTER (WHERE disposition = 'PENDING') AS pending_seconds
                  FROM ledger WHERE asset_id = :asset_id GROUP BY asset_id
                ),
                last_data AS (
                  SELECT max(received_at) AS last_received_at FROM (
                    SELECT received_at FROM test.test_execution WHERE asset_id = :asset_id
                    UNION ALL SELECT received_at FROM test.test_event WHERE asset_id = :asset_id
                    UNION ALL SELECT received_at FROM test.runtime_interval WHERE asset_id = :asset_id
                  ) facts
                )
                SELECT a.public_id, a.asset_code, a.asset_kind,
                       coalesce(nullif(a.display_name, ''), a.asset_code) AS display_name,
                       a.product_family, b.batch_code, a.serial_number, a.lifecycle_status,
                       current_execution.public_id AS execution_public_id,
                       current_execution.execution_code, current_execution.case_code,
                       current_execution.current_elapsed_seconds,
                       latest_health.health_status,
                       coalesce(d.total_seconds, 0) AS total_seconds,
                       coalesce(d.effective_seconds, 0) AS effective_seconds,
                       coalesce(d.excluded_seconds, 0) AS excluded_seconds,
                       coalesce(d.pending_seconds, 0) AS pending_seconds,
                       ld.last_received_at
                FROM test.asset a
                LEFT JOIN test.manufacturing_batch b ON b.id = a.batch_id
                LEFT JOIN durations d ON d.asset_id = a.id
                CROSS JOIN last_data ld
                LEFT JOIN LATERAL (
                  SELECT e.public_id, e.execution_code, tc.case_code,
                    CASE WHEN e.status = 'RUNNING' AND e.normalized_started_at IS NOT NULL
                      THEN greatest(0, extract(epoch FROM (
                        CASE
                          WHEN e.received_at IS NULL THEN e.normalized_started_at
                          WHEN e.received_at < :as_of_at - interval '5 minutes' THEN e.received_at
                          ELSE :as_of_at
                        END - e.normalized_started_at
                      )))
                      ELSE 0 END AS current_elapsed_seconds
                  FROM test.test_execution e
                  JOIN catalog.test_case_version tcv ON tcv.id = e.test_case_version_id
                  JOIN catalog.test_case tc ON tc.id = tcv.test_case_id
                  WHERE e.asset_id = a.id AND e.status IN ('RUNNING', 'PAUSED', 'BLOCKED')
                  ORDER BY (e.status = 'RUNNING') DESC,
                           e.normalized_started_at DESC NULLS LAST, e.id DESC LIMIT 1
                ) current_execution ON true
                LEFT JOIN LATERAL (
                  SELECT h.health_status FROM health.health_check_run h
                  WHERE h.asset_id = a.id
                  ORDER BY coalesce(h.completed_at, h.started_at, h.scheduled_at) DESC NULLS LAST,
                           h.id DESC LIMIT 1
                ) latest_health ON true
                WHERE a.id = :asset_id
                """
            ),
            {"asset_id": resolved["id"], "as_of_at": as_of_at},
        ).mappings().one()
        asset = self._asset_summary(row, as_of_at)
        context = self.session.execute(
            text(
                """
                SELECT c.campaign_code, cy.cycle_code, e.public_id AS execution_public_id,
                       e.execution_code, tc.case_code, e.status, e.normalized_started_at,
                       st.stage_code, st.stage_name, e.received_at
                FROM test.test_execution e
                JOIN test.test_campaign c ON c.id = e.campaign_id
                JOIN test.test_cycle cy ON cy.id = e.cycle_id
                JOIN catalog.test_case_version tcv ON tcv.id = e.test_case_version_id
                JOIN catalog.test_case tc ON tc.id = tcv.test_case_id
                LEFT JOIN LATERAL (
                  SELECT s.stage_code, s.stage_name FROM test.execution_stage s
                  WHERE s.execution_id = e.id AND s.status IN ('RUNNING', 'PAUSED', 'BLOCKED')
                  ORDER BY s.sequence_no DESC LIMIT 1
                ) st ON true
                WHERE e.asset_id = :asset_id
                  AND e.status IN ('RUNNING', 'PAUSED', 'BLOCKED')
                ORDER BY (e.status = 'RUNNING') DESC,
                         e.normalized_started_at DESC NULLS LAST, e.id DESC
                LIMIT 1
                """
            ),
            {"asset_id": resolved["id"]},
        ).mappings().first()
        if context:
            elapsed = _live_elapsed_seconds(
                as_of_at,
                context["normalized_started_at"],
                context["received_at"],
                context["status"],
            )
            current_context = CurrentContextView(
                campaign_code=context["campaign_code"],
                cycle_code=context["cycle_code"],
                execution_id=context["execution_public_id"],
                execution_code=context["execution_code"],
                test_case_code=context["case_code"],
                stage_code=context["stage_code"],
                stage_name=context["stage_name"],
                status=context["status"],
                started_at=context["normalized_started_at"],
                current_elapsed_seconds=elapsed,
                is_stale=_context_is_stale(
                    as_of_at, context["status"], context["received_at"]
                ),
            )
        else:
            current_context = CurrentContextView()
        configuration_row = self.session.execute(
            text(
                """
                SELECT public_id, fingerprint, effective_from, reliability_impact,
                       hardware_manifest, software_manifest, parameter_manifest
                FROM test.configuration_snapshot
                WHERE asset_id = :asset_id
                ORDER BY effective_from DESC, id DESC LIMIT 1
                """
            ),
            {"asset_id": resolved["id"]},
        ).mappings().first()
        configuration = (
            ConfigurationView(
                id=configuration_row["public_id"],
                fingerprint=configuration_row["fingerprint"],
                effective_from=configuration_row["effective_from"],
                reliability_impact=configuration_row["reliability_impact"],
                hardware_manifest=configuration_row["hardware_manifest"],
                software_manifest=configuration_row["software_manifest"],
                parameter_manifest=configuration_row["parameter_manifest"],
            )
            if configuration_row
            else ConfigurationView()
        )
        counts = self.session.execute(
            text(
                """
                SELECT
                  (SELECT count(*) FROM test.test_execution WHERE asset_id = :asset_id) AS execution_count,
                  (SELECT count(*) FROM test.test_event WHERE asset_id = :asset_id AND NOT voided) AS event_count,
                  (SELECT count(*) FROM integration.artifact WHERE asset_id = :asset_id) AS artifact_count,
                  (SELECT count(*) FROM integration.artifact
                   WHERE asset_id = :asset_id AND availability_status = 'AVAILABLE') AS available_count,
                  (SELECT count(*) FROM integration.artifact
                   WHERE asset_id = :asset_id AND availability_status IN ('MISSING', 'UNAVAILABLE_LEGACY')) AS missing_count
                """
            ),
            {"asset_id": resolved["id"]},
        ).mappings().one()
        health_rows = self.session.execute(
            text(
                """
                SELECT coalesce(health_status, 'INSUFFICIENT_EVIDENCE') AS status, count(*) AS count
                FROM health.health_check_run
                WHERE asset_id = :asset_id GROUP BY coalesce(health_status, 'INSUFFICIENT_EVIDENCE')
                """
            ),
            {"asset_id": resolved["id"]},
        ).mappings()
        return AssetDetail(
            asset=asset,
            current_context=current_context,
            configuration=configuration,
            execution_count=int(counts["execution_count"]),
            event_count=int(counts["event_count"]),
            health_summary={row["status"]: int(row["count"]) for row in health_rows},
            evidence_summary=EvidenceSummary(
                total_count=int(counts["artifact_count"]),
                available_count=int(counts["available_count"]),
                missing_count=int(counts["missing_count"]),
            ),
            as_of_at=as_of_at,
            data_cutoff_at=row["last_received_at"],
        )

    def asset_performance_trends(self, identifier: str) -> AssetPerformanceTrends:
        as_of_at = self._as_of()
        asset = self._resolve_asset(identifier)
        identity = self.session.execute(
            text(
                """
                SELECT public_id, asset_code,
                       coalesce(nullif(display_name, ''), asset_code) AS asset_name
                FROM test.asset
                WHERE id = :asset_id
                """
            ),
            {"asset_id": asset["id"]},
        ).mappings().one()
        execution = self.session.execute(
            text(
                """
                SELECT e.id, e.public_id, e.execution_code, tc.case_code
                FROM test.test_execution e
                JOIN catalog.test_case_version tcv ON tcv.id = e.test_case_version_id
                JOIN catalog.test_case tc ON tc.id = tcv.test_case_id
                WHERE e.asset_id = :asset_id
                  AND e.status <> 'VOIDED'
                  AND (
                    SELECT count(DISTINCT o.stage_id)
                    FROM health.metric_observation o
                    WHERE o.execution_id = e.id
                      AND o.asset_id = :asset_id
                      AND NOT o.voided
                      AND o.canonical_numeric_value IS NOT NULL
                  ) >= 4
                  AND (
                    SELECT count(DISTINCT o.metric_version_id)
                    FROM health.metric_observation o
                    WHERE o.execution_id = e.id
                      AND o.asset_id = :asset_id
                      AND NOT o.voided
                      AND o.canonical_numeric_value IS NOT NULL
                  ) >= 3
                ORDER BY coalesce(e.normalized_started_at, e.created_at) DESC, e.id DESC
                LIMIT 1
                """
            ),
            {"asset_id": asset["id"]},
        ).mappings().first()
        if not execution:
            return AssetPerformanceTrends(
                asset_id=identity["public_id"],
                asset_code=identity["asset_code"],
                asset_name=identity["asset_name"],
                metrics=[],
                as_of_at=as_of_at,
            )

        rows = list(
            self.session.execute(
                text(
                    """
                    WITH selected_metrics AS (
                      SELECT o.metric_version_id, d.metric_code
                      FROM health.metric_observation o
                      JOIN catalog.metric_version mv ON mv.id = o.metric_version_id
                      JOIN catalog.metric_definition d ON d.id = mv.metric_definition_id
                      WHERE o.asset_id = :asset_id
                        AND o.execution_id = :execution_id
                        AND NOT o.voided
                        AND o.canonical_numeric_value IS NOT NULL
                      GROUP BY o.metric_version_id, d.metric_code
                      HAVING count(DISTINCT o.stage_id) >= 4
                      ORDER BY d.metric_code
                      LIMIT 4
                    ),
                    ranked AS (
                      SELECT d.metric_code, d.canonical_name AS metric_name,
                             mv.canonical_unit, s.stage_code, s.stage_name,
                             s.sequence_no, s.status AS stage_status,
                             o.observed_at, o.canonical_numeric_value,
                             o.quality_status,
                             coalesce(
                               nullif(o.exposure_coordinates ->> 'stage_elapsed_seconds', '')::numeric,
                               extract(epoch FROM (o.observed_at - s.normalized_started_at))
                             ) AS elapsed_seconds,
                             coalesce(
                               nullif(o.exposure_coordinates ->> 'stage_progress_percent', '')::numeric,
                               0
                             ) AS progress_percent,
                             row_number() OVER (
                               PARTITION BY o.metric_version_id, o.stage_id
                               ORDER BY o.observed_at, o.id
                             ) AS point_rank
                      FROM health.metric_observation o
                      JOIN selected_metrics selected
                        ON selected.metric_version_id = o.metric_version_id
                      JOIN catalog.metric_version mv ON mv.id = o.metric_version_id
                      JOIN catalog.metric_definition d ON d.id = mv.metric_definition_id
                      JOIN test.execution_stage s ON s.id = o.stage_id
                      WHERE o.asset_id = :asset_id
                        AND o.execution_id = :execution_id
                        AND NOT o.voided
                        AND o.canonical_numeric_value IS NOT NULL
                    )
                    SELECT *, max(observed_at) OVER () AS data_cutoff_at
                    FROM ranked
                    WHERE point_rank <= 120
                    ORDER BY metric_code, sequence_no, observed_at
                    """
                ),
                {"asset_id": asset["id"], "execution_id": execution["id"]},
            ).mappings()
        )
        metrics: list[PerformanceTrendMetric] = []
        metric_groups: dict[str, PerformanceTrendMetric] = {}
        series_values: dict[tuple[str, str], list[float]] = {}
        for row in rows:
            metric = metric_groups.get(row["metric_code"])
            if metric is None:
                metric = PerformanceTrendMetric(
                    metric_code=row["metric_code"],
                    metric_name=row["metric_name"],
                    unit=row["canonical_unit"],
                )
                metric_groups[row["metric_code"]] = metric
                metrics.append(metric)
            key = (row["metric_code"], row["stage_code"])
            if not metric.series or metric.series[-1].stage_code != row["stage_code"]:
                metric.series.append(
                    PerformanceTrendSeries(
                        stage_code=row["stage_code"],
                        stage_name=row["stage_name"],
                        sequence_no=int(row["sequence_no"]),
                        status=row["stage_status"],
                        min_value=0,
                        max_value=0,
                        avg_value=0,
                    )
                )
                series_values[key] = []
            value = _number(row["canonical_numeric_value"])
            series_values[key].append(value)
            metric.series[-1].points.append(
                PerformanceTrendPoint(
                    observed_at=row["observed_at"],
                    elapsed_seconds=_number(row["elapsed_seconds"]),
                    progress_percent=_number(row["progress_percent"]),
                    value=value,
                    quality_status=row["quality_status"],
                )
            )
        for metric in metrics:
            for series in metric.series:
                values = series_values[(metric.metric_code, series.stage_code)]
                series.min_value = min(values)
                series.max_value = max(values)
                series.avg_value = sum(values) / len(values)
        return AssetPerformanceTrends(
            asset_id=identity["public_id"],
            asset_code=identity["asset_code"],
            asset_name=identity["asset_name"],
            execution_id=execution["public_id"],
            execution_code=execution["execution_code"],
            test_case_code=execution["case_code"],
            metrics=metrics,
            as_of_at=as_of_at,
            data_cutoff_at=rows[0]["data_cutoff_at"] if rows else None,
        )

    def _execution_list(
        self,
        *,
        asset_id: int | None,
        test_case_id: int | None,
        limit: int,
        cursor: str | None,
        as_of_at: datetime | None = None,
    ) -> ExecutionList:
        as_of_at = as_of_at or self._as_of()
        (after_id,) = _decode_cursor(cursor)
        rows = list(
            self.session.execute(
                text(
                    """
                    SELECT e.id AS internal_id, e.public_id, e.execution_code,
                           a.public_id AS asset_public_id, a.asset_code,
                           tc.public_id AS case_public_id, tc.case_code, tc.name AS case_name,
                           c.campaign_code, cy.cycle_code, e.status,
                           e.normalized_started_at, e.normalized_ended_at,
                           e.data_quality, e.clock_quality, e.received_at,
                           max(greatest(
                             e.received_at, runtime_totals.last_received_at
                           )) OVER () AS all_data_cutoff_at,
                           coalesce(stage_totals.stage_count, 0) AS stage_count,
                           coalesce(runtime_totals.closed_duration_seconds, 0) AS closed_duration_seconds,
                           CASE WHEN e.status = 'RUNNING' AND e.normalized_started_at IS NOT NULL
                             THEN greatest(0, extract(epoch FROM (
                               CASE
                                 WHEN e.received_at IS NULL THEN e.normalized_started_at
                                 WHEN e.received_at < :as_of_at - interval '5 minutes' THEN e.received_at
                                 ELSE :as_of_at
                               END - e.normalized_started_at
                             )))
                             ELSE 0 END AS active_elapsed_seconds
                    FROM test.test_execution e
                    JOIN test.asset a ON a.id = e.asset_id
                    JOIN test.test_campaign c ON c.id = e.campaign_id
                    JOIN test.test_cycle cy ON cy.id = e.cycle_id
                    JOIN catalog.test_case_version tcv ON tcv.id = e.test_case_version_id
                    JOIN catalog.test_case tc ON tc.id = tcv.test_case_id
                    LEFT JOIN LATERAL (
                      SELECT count(*) AS stage_count
                      FROM test.execution_stage stage WHERE stage.execution_id = e.id
                    ) stage_totals ON true
                    LEFT JOIN LATERAL (
                      SELECT sum(ri.active_seconds) AS closed_duration_seconds,
                             max(ri.received_at) AS last_received_at
                      FROM test.runtime_interval ri
                      WHERE ri.execution_id = e.id AND NOT ri.voided
                    ) runtime_totals ON true
                    WHERE e.id > :after_id AND e.status <> 'VOIDED'
                      AND (CAST(:asset_id AS bigint) IS NULL OR e.asset_id = CAST(:asset_id AS bigint))
                      AND (CAST(:test_case_id AS bigint) IS NULL
                           OR tcv.test_case_id = CAST(:test_case_id AS bigint))
                    ORDER BY e.id LIMIT :fetch_limit
                    """
                ),
                {
                    "as_of_at": as_of_at,
                    "after_id": after_id,
                    "asset_id": asset_id,
                    "test_case_id": test_case_id,
                    "fetch_limit": limit + 1,
                },
            ).mappings()
        )
        has_more = len(rows) > limit
        rows = rows[:limit]
        items = [self._execution_item(row) for row in rows]
        return ExecutionList(
            items=items,
            page=PageMeta(
                limit=limit,
                next_cursor=_encode_cursor(rows[-1]["internal_id"]) if has_more and rows else None,
            ),
            as_of_at=as_of_at,
            data_cutoff_at=rows[0]["all_data_cutoff_at"] if rows else None,
        )

    @staticmethod
    def _execution_item(row: dict) -> ExecutionListItem:
        return ExecutionListItem(
            id=row["public_id"],
            code=row["execution_code"],
            asset_id=row["asset_public_id"],
            asset_code=row["asset_code"],
            test_case_id=row["case_public_id"],
            test_case_code=row["case_code"],
            test_case_name=row["case_name"],
            campaign_code=row["campaign_code"],
            cycle_code=row["cycle_code"],
            stage_count=int(row["stage_count"]),
            status=row["status"],
            started_at=row["normalized_started_at"],
            ended_at=row["normalized_ended_at"],
            closed_duration_seconds=_number(row["closed_duration_seconds"]),
            active_elapsed_seconds=_number(row["active_elapsed_seconds"]),
            data_quality=row["data_quality"],
            clock_quality=row["clock_quality"],
        )

    def asset_executions(self, identifier: str, limit: int, cursor: str | None) -> ExecutionList:
        asset = self._resolve_asset(identifier)
        return self._execution_list(
            asset_id=asset["id"], test_case_id=None, limit=limit, cursor=cursor
        )

    def asset_events(self, identifier: str, limit: int, cursor: str | None) -> EventList:
        as_of_at = self._as_of()
        asset = self._resolve_asset(identifier)
        (after_id,) = _decode_cursor(cursor)
        rows = list(
            self.session.execute(
                text(
                    """
                    SELECT e.id AS internal_id, e.public_id, e.event_type, e.normalized_time,
                           execution.public_id AS execution_public_id,
                           e.data_quality, e.clock_quality, e.payload,
                           i.classification, i.review_status,
                           max(e.received_at) OVER () AS all_data_cutoff_at
                    FROM test.test_event e
                    LEFT JOIN test.test_execution execution ON execution.id = e.execution_id
                    LEFT JOIN reliability.interruption i ON i.event_id = e.id AND NOT i.voided
                    WHERE e.asset_id = :asset_id AND e.id > :after_id AND NOT e.voided
                    ORDER BY e.id LIMIT :fetch_limit
                    """
                ),
                {"asset_id": asset["id"], "after_id": after_id, "fetch_limit": limit + 1},
            ).mappings()
        )
        has_more = len(rows) > limit
        rows = rows[:limit]
        return EventList(
            items=[
                EventListItem(
                    id=row["public_id"],
                    event_type=row["event_type"],
                    normalized_time=row["normalized_time"],
                    execution_id=row["execution_public_id"],
                    data_quality=row["data_quality"],
                    clock_quality=row["clock_quality"],
                    payload=row["payload"],
                    interruption_classification=row["classification"],
                    interruption_review_status=row["review_status"],
                )
                for row in rows
            ],
            page=PageMeta(
                limit=limit,
                next_cursor=_encode_cursor(rows[-1]["internal_id"]) if has_more and rows else None,
            ),
            as_of_at=as_of_at,
            data_cutoff_at=rows[0]["all_data_cutoff_at"] if rows else None,
        )

    def _resolve_test_case(self, identifier: str) -> dict:
        row = self.session.execute(
            text(
                """
                SELECT tc.id, tc.public_id, tc.case_code, tc.name, tc.asset_kind,
                       tc.target_part_code, part.name AS target_part_name,
                       tc.domain, tc.evidence_type, tc.enabled
                FROM catalog.test_case tc
                LEFT JOIN catalog.test_target_part part
                  ON part.part_code = tc.target_part_code
                WHERE tc.public_id::text = :identifier OR tc.case_code = :identifier
                """
            ),
            {"identifier": identifier},
        ).mappings().first()
        if not row:
            raise ApiError(404, "not_found", "未找到 Test Case。")
        return dict(row)

    def get_test_case(
        self, identifier: str, *, limit: int, cursor: str | None
    ) -> TestCaseDetail:
        as_of_at = self._as_of()
        case = self._resolve_test_case(identifier)
        aggregate = self.session.execute(
            text(
                f"""
                WITH {LEDGER_CTE},
                case_executions AS (
                  SELECT e.id, e.status, e.normalized_started_at, e.received_at
                  FROM test.test_execution e
                  JOIN catalog.test_case_version tcv ON tcv.id = e.test_case_version_id
                  WHERE tcv.test_case_id = :case_id AND e.status <> 'VOIDED'
                ),
                execution_totals AS (
                  SELECT ce.id, ce.status, ce.normalized_started_at, ce.received_at,
                    coalesce(sum(l.active_seconds), 0) AS total_seconds,
                    coalesce(sum(l.active_seconds) FILTER (WHERE l.disposition = 'INCLUDED'), 0) AS effective_seconds,
                    coalesce(sum(l.active_seconds) FILTER (WHERE l.disposition = 'EXCLUDED'), 0) AS excluded_seconds,
                    coalesce(sum(l.active_seconds) FILTER (WHERE l.disposition = 'PENDING'), 0) AS pending_seconds,
                    max(l.received_at) AS runtime_received_at
                  FROM case_executions ce
                  LEFT JOIN ledger l ON l.execution_id = ce.id
                  GROUP BY ce.id, ce.status, ce.normalized_started_at, ce.received_at
                )
                SELECT
                  coalesce(sum(et.total_seconds), 0) AS total_seconds,
                  coalesce(sum(et.effective_seconds), 0) AS effective_seconds,
                  coalesce(sum(et.excluded_seconds), 0) AS excluded_seconds,
                  coalesce(sum(et.pending_seconds), 0) AS pending_seconds,
                  coalesce(sum(CASE WHEN et.status = 'RUNNING'
                    THEN greatest(0, extract(epoch FROM (
                      CASE
                        WHEN et.received_at IS NULL THEN et.normalized_started_at
                        WHEN et.received_at < :as_of_at - interval '5 minutes' THEN et.received_at
                        ELSE :as_of_at
                      END - et.normalized_started_at
                    )))
                    ELSE 0 END), 0) AS active_elapsed_seconds,
                  count(*) FILTER (WHERE et.status = 'RUNNING') AS running_count,
                  count(*) FILTER (WHERE et.status = 'COMPLETED') AS completed_count,
                  count(*) FILTER (WHERE et.status = 'FAILED') AS failed_count,
                  count(*) FILTER (WHERE et.status = 'BLOCKED') AS blocked_count,
                  greatest(
                    max(et.received_at), max(et.runtime_received_at)
                  ) AS data_cutoff_at,
                  (SELECT tcv.version FROM catalog.test_case_version tcv
                   WHERE tcv.test_case_id = :case_id
                   ORDER BY (tcv.status = 'PUBLISHED') DESC, tcv.id DESC LIMIT 1) AS latest_version
                FROM execution_totals et
                """
            ),
            {"case_id": case["id"], "as_of_at": as_of_at},
        ).mappings().one()
        failure_row = self.session.execute(
            text(
                """
                WITH overall_scope AS (
                  SELECT id FROM reliability.mtbf_scope
                  WHERE scope_code = 'OVERALL' AND asset_kind = :asset_kind
                    AND status = 'PUBLISHED'
                  ORDER BY version DESC LIMIT 1
                )
                SELECT
                  count(DISTINCT coalesce(i.failure_group_key, i.public_id::text)) FILTER (
                    WHERE i.classification = 'FAILED'
                      AND i.review_status IN ('CONFIRMED', 'RECLASSIFIED')
                      AND isa.status = 'INCLUDED'
                  ) AS relevant_count,
                  count(DISTINCT coalesce(i.failure_group_key, i.public_id::text)) FILTER (
                    WHERE i.classification = 'BLOCKED' OR i.review_status = 'PENDING'
                      OR isa.status = 'PENDING'
                  ) AS pending_count
                FROM reliability.interruption i
                JOIN test.test_event ev ON ev.id = i.event_id
                JOIN test.test_execution e ON e.id = ev.execution_id
                JOIN catalog.test_case_version tcv ON tcv.id = e.test_case_version_id
                LEFT JOIN overall_scope os ON true
                LEFT JOIN reliability.interruption_scope_assignment isa
                  ON isa.interruption_id = i.id AND isa.scope_id = os.id
                WHERE tcv.test_case_id = :case_id AND NOT i.voided
                """
            ),
            {"case_id": case["id"], "asset_kind": case["asset_kind"]},
        ).mappings().one()
        failure_count = int(failure_row["relevant_count"])
        exposure_hours = _number(aggregate["effective_seconds"]) / 3600
        calculation = calculate_mtbf(exposure_hours, int(failure_count))
        observation = MtbfObservationView(
            scope="OVERALL_TEST_CASE_OBSERVED",
            method=IMPLEMENTATION_KEY,
            data_cutoff_at=aggregate["data_cutoff_at"],
            effective_exposure_seconds=_number(aggregate["effective_seconds"]),
            relevant_failure_count=int(failure_count),
            pending_failure_count=int(failure_row["pending_count"]),
            pending_exposure_seconds=_number(aggregate["pending_seconds"]),
            point_estimate_hours=calculation.point_estimate_hours,
            point_estimate_status=calculation.point_estimate_status,
            lower_bounds_hours={
                "0.7": calculation.lower_bound(0.70),
                "0.9": calculation.lower_bound(0.90),
            },
        )
        executions = self._execution_list(
            asset_id=None,
            test_case_id=case["id"],
            limit=limit,
            cursor=cursor,
            as_of_at=as_of_at,
        )
        return TestCaseDetail(
            id=case["public_id"],
            code=case["case_code"],
            name=case["name"],
            asset_kind=case["asset_kind"],
            target_part_code=case["target_part_code"],
            target_part_name=case["target_part_name"],
            domain=case["domain"],
            evidence_type=case["evidence_type"],
            enabled=case["enabled"],
            latest_version=aggregate["latest_version"],
            status_counts={
                "RUNNING": int(aggregate["running_count"]),
                "COMPLETED": int(aggregate["completed_count"]),
                "FAILED": int(aggregate["failed_count"]),
                "BLOCKED": int(aggregate["blocked_count"]),
            },
            durations=DurationTotals(
                total_duration_seconds=_number(aggregate["total_seconds"]),
                effective_exposure_seconds=_number(aggregate["effective_seconds"]),
                excluded_seconds=_number(aggregate["excluded_seconds"]),
                pending_seconds=_number(aggregate["pending_seconds"]),
                active_elapsed_seconds=_number(aggregate["active_elapsed_seconds"]),
            ),
            mtbf_observation=observation,
            executions=executions,
            as_of_at=as_of_at,
            data_cutoff_at=aggregate["data_cutoff_at"],
        )

    def _resolve_execution(self, identifier: str) -> dict:
        row = self.session.execute(
            text(
                """
                SELECT id, public_id, execution_code
                FROM test.test_execution
                WHERE public_id::text = :identifier OR execution_code = :identifier
                """
            ),
            {"identifier": identifier},
        ).mappings().first()
        if not row:
            raise ApiError(404, "not_found", "未找到 Execution，或当前用户无权访问。")
        return dict(row)

    def get_execution_joint_analysis(
        self,
        identifier: str,
        subject_code: str | None = None,
        stage_code: str | None = None,
        cycle_index: int | None = None,
    ) -> ExecutionJointAnalysis:
        as_of_at = self._as_of()
        execution = self._resolve_execution(identifier)
        metadata_rows = self.session.execute(
            text(
                """
                SELECT ms.id AS series_internal_id,
                       ms.public_id AS series_public_id,
                       subject.subject_code,
                       subject.name AS subject_name,
                       subject.subject_kind,
                       subject.target_part_code,
                       subject.display_order AS subject_display_order,
                       subject.enabled AS subject_enabled,
                       stage.public_id AS stage_public_id,
                       stage.stage_code,
                       stage.stage_name,
                       stage.sequence_no AS stage_sequence_no,
                       stage.status AS stage_status,
                       definition.metric_code,
                       definition.canonical_name AS metric_name,
                       version.canonical_unit,
                       ms.series_kind,
                       ms.cycle_index,
                       ms.point_count,
                       ms.started_at,
                       ms.ended_at,
                       ms.sampling_interval_ms,
                       ms.downsample_method,
                       ms.quality_status
                FROM health.metric_series ms
                JOIN catalog.telemetry_subject subject
                  ON subject.subject_code = ms.subject_code
                JOIN test.execution_stage stage ON stage.id = ms.stage_id
                JOIN catalog.metric_version version
                  ON version.id = ms.metric_version_id
                JOIN catalog.metric_definition definition
                  ON definition.id = version.metric_definition_id
                WHERE ms.execution_id = :execution_id
                  AND subject.enabled
                  AND definition.metric_code = ANY(CAST(:metric_codes AS text[]))
                ORDER BY subject.display_order, subject.subject_code,
                         (stage.stage_code = 'STEADY_RUN') DESC,
                         stage.sequence_no, ms.cycle_index DESC,
                         definition.metric_code, ms.series_kind, ms.id
                """
            ),
            {
                "execution_id": execution["id"],
                "metric_codes": list(JOINT_METRIC_CODES),
            },
        ).mappings()
        metadata = [
            _JointSeriesMetadata(
                internal_id=int(row["series_internal_id"]),
                public_id=row["series_public_id"],
                subject_code=row["subject_code"],
                subject_name=row["subject_name"],
                subject_kind=row["subject_kind"],
                target_part_code=row["target_part_code"],
                subject_display_order=int(row["subject_display_order"]),
                subject_enabled=bool(row["subject_enabled"]),
                stage_public_id=row["stage_public_id"],
                stage_code=row["stage_code"],
                stage_name=row["stage_name"],
                stage_sequence_no=int(row["stage_sequence_no"]),
                stage_status=row["stage_status"],
                metric_code=row["metric_code"],
                metric_name=row["metric_name"],
                unit=row["canonical_unit"],
                series_kind=row["series_kind"],
                cycle_index=int(row["cycle_index"]),
                point_count=int(row["point_count"]),
                started_at=row["started_at"],
                ended_at=row["ended_at"],
                sampling_interval_ms=(
                    int(row["sampling_interval_ms"])
                    if row["sampling_interval_ms"] is not None
                    else None
                ),
                downsample_method=row["downsample_method"],
                quality_status=row["quality_status"],
            )
            for row in metadata_rows
        ]
        selection = _resolve_joint_selection(
            metadata,
            subject_code=subject_code,
            stage_code=stage_code,
            cycle_index=cycle_index,
        )

        subject_by_code: dict[str, JointAnalysisSubject] = {}
        for item in sorted(
            metadata,
            key=lambda value: (
                value.subject_display_order,
                value.subject_code,
            ),
        ):
            subject_by_code.setdefault(
                item.subject_code,
                JointAnalysisSubject(
                    subject_code=item.subject_code,
                    name=item.subject_name,
                    subject_kind=item.subject_kind,
                    target_part_code=item.target_part_code,
                    display_order=item.subject_display_order,
                    enabled=item.subject_enabled,
                ),
            )

        selected_subject_metadata = [
            item
            for item in metadata
            if item.subject_code == selection.subject_code
        ]
        stage_by_code: dict[str, JointAnalysisStage] = {}
        for item in sorted(
            selected_subject_metadata,
            key=lambda value: (
                value.stage_code != "STEADY_RUN",
                value.stage_sequence_no,
                value.stage_code,
            ),
        ):
            stage_by_code.setdefault(
                item.stage_code,
                JointAnalysisStage(
                    id=item.stage_public_id,
                    stage_code=item.stage_code,
                    stage_name=item.stage_name,
                    sequence_no=item.stage_sequence_no,
                    status=item.stage_status,
                ),
            )

        selected_stage_metadata = [
            item
            for item in selected_subject_metadata
            if item.stage_code == selection.stage_code
        ]
        cycles: list[JointAnalysisCycle] = []
        for available_cycle in sorted(
            {item.cycle_index for item in selected_stage_metadata},
            reverse=True,
        ):
            cycle_metadata = [
                item
                for item in selected_stage_metadata
                if item.cycle_index == available_cycle
            ]
            starts = [
                item.started_at
                for item in cycle_metadata
                if item.started_at is not None
            ]
            ends = [
                item.ended_at
                for item in cycle_metadata
                if item.ended_at is not None
            ]
            statuses = {item.quality_status for item in cycle_metadata}
            if statuses == {"VALID"}:
                cycle_quality = "VALID"
            elif statuses == {"MISSING"}:
                cycle_quality = "MISSING"
            elif statuses == {"INVALID"}:
                cycle_quality = "INVALID"
            else:
                cycle_quality = "PARTIAL"
            cycles.append(
                JointAnalysisCycle(
                    cycle_index=available_cycle,
                    started_at=min(starts) if starts else None,
                    ended_at=max(ends) if ends else None,
                    quality_status=cycle_quality,
                    series_count=len(cycle_metadata),
                )
            )

        selected_metadata = [
            item
            for item in selected_stage_metadata
            if item.cycle_index == selection.cycle_index
        ]
        metric_by_code: dict[str, JointAnalysisMetric] = {}
        for item in sorted(
            selected_metadata,
            key=lambda value: (
                _JOINT_METRIC_ORDER.get(value.metric_code, len(JOINT_METRIC_CODES)),
                value.metric_code,
            ),
        ):
            metric_by_code.setdefault(
                item.metric_code,
                JointAnalysisMetric(
                    metric_code=item.metric_code,
                    metric_name=item.metric_name,
                    unit=item.unit,
                ),
            )

        point_rows = []
        if selected_metadata:
            point_rows = list(
                self.session.execute(
                    text(
                        """
                        SELECT o.series_id, o.sample_index,
                               o.canonical_numeric_value,
                               o.observed_at, o.quality_status
                        FROM health.metric_observation o
                        WHERE o.execution_id = :execution_id
                          AND o.series_id = ANY(CAST(:series_ids AS bigint[]))
                          AND NOT o.voided
                        ORDER BY o.series_id, o.sample_index, o.id
                        """
                    ),
                    {
                        "execution_id": execution["id"],
                        "series_ids": [
                            item.internal_id for item in selected_metadata
                        ],
                    },
                ).mappings()
            )

        statistic_points_by_series: dict[int, list[JointStatisticPoint]] = {
            item.internal_id: [] for item in selected_metadata
        }
        for row in point_rows:
            statistic_points_by_series[int(row["series_id"])].append(
                JointStatisticPoint(
                    sample_index=int(row["sample_index"]),
                    value=(
                        float(row["canonical_numeric_value"])
                        if row["canonical_numeric_value"] is not None
                        else None
                    ),
                    observed_at=row["observed_at"],
                    quality_status=row["quality_status"],
                )
            )

        response_series: list[JointAnalysisSeries] = []
        statistic_series: list[JointStatisticSeries] = []
        for item in sorted(
            selected_metadata,
            key=lambda value: (
                _JOINT_METRIC_ORDER.get(value.metric_code, len(JOINT_METRIC_CODES)),
                value.metric_code,
                value.series_kind,
                value.internal_id,
            ),
        ):
            raw_points = sorted(
                statistic_points_by_series[item.internal_id],
                key=lambda point: point.sample_index,
            )
            statistic_series.append(
                JointStatisticSeries(
                    metric_code=item.metric_code,
                    unit=item.unit,
                    quality_status=item.quality_status,
                    started_at=item.started_at,
                    ended_at=item.ended_at,
                    points=tuple(raw_points),
                )
            )
            first_sample_index = raw_points[0].sample_index if raw_points else 0
            first_observed_at = raw_points[0].observed_at if raw_points else None
            if (
                item.started_at is not None
                and item.ended_at is not None
                and item.ended_at > item.started_at
            ):
                duration_seconds = (
                    item.ended_at - item.started_at
                ).total_seconds()
            elif len(raw_points) > 1:
                duration_seconds = max(
                    0.0,
                    (
                        raw_points[-1].observed_at
                        - raw_points[0].observed_at
                    ).total_seconds(),
                )
            else:
                duration_seconds = 0.0
            display_candidates: list[JointAnalysisPoint] = []
            for point in raw_points:
                if item.started_at is not None:
                    elapsed_seconds = max(
                        0.0,
                        (point.observed_at - item.started_at).total_seconds(),
                    )
                elif item.sampling_interval_ms is not None:
                    elapsed_seconds = max(
                        0.0,
                        (
                            point.sample_index - first_sample_index
                        )
                        * item.sampling_interval_ms
                        / 1000,
                    )
                elif first_observed_at is not None:
                    elapsed_seconds = max(
                        0.0,
                        (point.observed_at - first_observed_at).total_seconds(),
                    )
                else:
                    elapsed_seconds = 0.0
                progress_percent = (
                    min(100.0, max(0.0, elapsed_seconds / duration_seconds * 100))
                    if duration_seconds > 0
                    else None
                )
                display_candidates.append(
                    JointAnalysisPoint(
                        sample_index=point.sample_index,
                        elapsed_seconds=elapsed_seconds,
                        progress_percent=progress_percent,
                        value=point.value,
                        observed_at=point.observed_at,
                        quality_status=point.quality_status,
                    )
                )
            display_points = _downsample_points(display_candidates, 120)
            response_series.append(
                JointAnalysisSeries(
                    id=item.public_id,
                    metric_code=item.metric_code,
                    metric_name=item.metric_name,
                    unit=item.unit,
                    subject_code=item.subject_code,
                    stage_code=item.stage_code,
                    series_kind=item.series_kind,
                    cycle_index=item.cycle_index,
                    point_count=item.point_count,
                    display_point_count=len(display_points),
                    started_at=item.started_at,
                    ended_at=item.ended_at,
                    sampling_interval_ms=item.sampling_interval_ms,
                    downsample_method=item.downsample_method,
                    quality_status=item.quality_status,
                    points=display_points,
                )
            )

        observed_cutoff = max(
            (point["observed_at"] for point in point_rows),
            default=None,
        )
        series_cutoff = max(
            (
                item.ended_at
                for item in selected_metadata
                if item.ended_at is not None
            ),
            default=None,
        )
        return ExecutionJointAnalysis(
            execution_id=execution["public_id"],
            execution_code=execution["execution_code"],
            subjects=list(subject_by_code.values()),
            stages=list(stage_by_code.values()),
            cycles=cycles,
            metrics=list(metric_by_code.values()),
            resolved_selection=selection,
            summary=calculate_joint_summary(statistic_series),
            series=response_series,
            as_of_at=as_of_at,
            data_cutoff_at=observed_cutoff or series_cutoff,
        )

    def get_execution(self, identifier: str) -> ExecutionDetail:
        as_of_at = self._as_of()
        row = self.session.execute(
            text(
                """
                SELECT e.id AS internal_id, e.public_id, e.execution_code,
                       a.public_id AS asset_public_id, a.asset_code,
                       tc.public_id AS case_public_id, tc.case_code, tc.name AS case_name,
                       c.campaign_code, cy.cycle_code, e.status,
                       e.normalized_started_at, e.normalized_ended_at,
                       e.data_quality, e.clock_quality, e.received_at,
                       greatest(e.received_at, runtime_totals.last_received_at)
                         AS fact_data_cutoff_at,
                       coalesce(stage_totals.stage_count, 0) AS stage_count,
                       coalesce(runtime_totals.closed_duration_seconds, 0) AS closed_duration_seconds,
                       CASE WHEN e.status = 'RUNNING' AND e.normalized_started_at IS NOT NULL
                         THEN greatest(0, extract(epoch FROM (
                           CASE
                             WHEN e.received_at IS NULL THEN e.normalized_started_at
                             WHEN e.received_at < :as_of_at - interval '5 minutes' THEN e.received_at
                             ELSE :as_of_at
                           END - e.normalized_started_at
                         )))
                         ELSE 0 END AS active_elapsed_seconds,
                       cfg.public_id AS configuration_public_id, cfg.fingerprint,
                       cfg.effective_from, cfg.reliability_impact, cfg.hardware_manifest,
                       cfg.software_manifest, cfg.parameter_manifest,
                       station.station_code, station.name AS station_name,
                       operator.display_name AS operator_name, source.source_code
                FROM test.test_execution e
                JOIN test.asset a ON a.id = e.asset_id
                JOIN test.test_campaign c ON c.id = e.campaign_id
                JOIN test.test_cycle cy ON cy.id = e.cycle_id
                JOIN catalog.test_case_version tcv ON tcv.id = e.test_case_version_id
                JOIN catalog.test_case tc ON tc.id = tcv.test_case_id
                JOIN test.configuration_snapshot cfg ON cfg.id = e.configuration_snapshot_id
                LEFT JOIN test.station station ON station.id = e.station_id
                LEFT JOIN iam.app_user operator ON operator.id = e.operator_id
                LEFT JOIN integration.ingestion_source source ON source.id = e.source_id
                LEFT JOIN LATERAL (
                  SELECT count(*) AS stage_count
                  FROM test.execution_stage stage WHERE stage.execution_id = e.id
                ) stage_totals ON true
                LEFT JOIN LATERAL (
                  SELECT sum(ri.active_seconds) AS closed_duration_seconds,
                         max(ri.received_at) AS last_received_at
                  FROM test.runtime_interval ri
                  WHERE ri.execution_id = e.id AND NOT ri.voided
                ) runtime_totals ON true
                WHERE e.public_id::text = :identifier OR e.execution_code = :identifier
                """
            ),
            {"identifier": identifier, "as_of_at": as_of_at},
        ).mappings().first()
        if not row:
            raise ApiError(404, "not_found", "未找到 Execution，或当前用户无权访问。")
        result_row = self.session.execute(
            text(
                """
                SELECT source_status, outcome, termination_kind, summary, issues,
                       exception_count, reported_duration_seconds,
                       executor_display, environment_label, last_data_at,
                       telemetry_source, archive_status, archive_path,
                       report_reference, normalization_flags
                FROM test.execution_result
                WHERE execution_id = :execution_id
                """
            ),
            {"execution_id": row["internal_id"]},
        ).mappings().first()
        execution_result = (
            ExecutionResultView(**dict(result_row)) if result_row else None
        )
        metrics_rows = list(
            self.session.execute(
                text(
                    """
                    SELECT o.public_id, d.metric_code, d.canonical_name,
                           o.exposure_coordinates ->> 'joint' AS joint,
                           o.canonical_numeric_value, o.canonical_text_value,
                           v.canonical_unit, o.observed_at, o.quality_status,
                           v.formal_eligible
                    FROM health.metric_observation o
                    JOIN catalog.metric_version v ON v.id = o.metric_version_id
                    JOIN catalog.metric_definition d ON d.id = v.metric_definition_id
                    WHERE o.execution_id = :execution_id
                      AND o.series_id IS NULL
                      AND NOT o.voided
                    ORDER BY d.metric_code, joint NULLS FIRST, o.observed_at
                    """
                ),
                {"execution_id": row["internal_id"]},
            ).mappings()
        )
        metrics = [
            MetricObservationView(
                id=item["public_id"],
                metric_code=item["metric_code"],
                metric_name=item["canonical_name"],
                joint=item["joint"],
                value=(
                    float(item["canonical_numeric_value"])
                    if item["canonical_numeric_value"] is not None
                    else None
                ),
                text_value=item["canonical_text_value"],
                unit=item["canonical_unit"],
                observed_at=item["observed_at"],
                quality_status=item["quality_status"],
                formal_eligible=item["formal_eligible"],
            )
            for item in metrics_rows
        ]
        peaks: dict[str, MetricPeakView] = {}
        for item in metrics:
            if item.value is None:
                continue
            current = peaks.get(item.metric_code)
            if current is None or item.value > current.value:
                peaks[item.metric_code] = MetricPeakView(
                    metric_code=item.metric_code,
                    metric_name=item.metric_name,
                    value=item.value,
                    unit=item.unit,
                    observed_at=item.observed_at,
                )
        evidence_rows = self.session.execute(
            text(
                """
                SELECT public_id AS id, kind, file_name, mime_type, size_bytes,
                       availability_status, sha256
                FROM integration.artifact
                WHERE execution_id = :execution_id ORDER BY created_at DESC, id DESC
                """
            ),
            {"execution_id": row["internal_id"]},
        ).mappings()
        evidence = [ArtifactView(**dict(item)) for item in evidence_rows]
        quality_counts: dict[str, int] = {}
        for item in metrics:
            quality_counts[item.quality_status] = quality_counts.get(item.quality_status, 0) + 1
        return ExecutionDetail(
            execution=self._execution_item(row),
            configuration=ConfigurationView(
                id=row["configuration_public_id"],
                fingerprint=row["fingerprint"],
                effective_from=row["effective_from"],
                reliability_impact=row["reliability_impact"],
                hardware_manifest=row["hardware_manifest"],
                software_manifest=row["software_manifest"],
                parameter_manifest=row["parameter_manifest"],
            ),
            station_code=row["station_code"],
            station_name=row["station_name"],
            operator_name=row["operator_name"],
            source_code=row["source_code"],
            result=execution_result,
            peak_summary=list(peaks.values()),
            metrics=metrics,
            data_quality_summary={
                "execution_data_quality": row["data_quality"],
                "execution_clock_quality": row["clock_quality"],
                "metric_quality_counts": quality_counts,
            },
            evidence=evidence,
            as_of_at=as_of_at,
            data_cutoff_at=row["fact_data_cutoff_at"],
        )

    def mtbf_conclusions(
        self,
        *,
        asset_kind: str | None,
        limit: int,
        cursor: str | None,
    ) -> MtbfConclusionList:
        as_of_at = self._as_of()
        (after_id,) = _decode_cursor(cursor)
        rows = list(
            self.session.execute(
                text(
                    """
                    SELECT cfg.id AS internal_id, c.public_id AS campaign_public_id,
                           c.campaign_code, c.name AS campaign_name, c.asset_kind,
                           s.scope_code, s.name AS scope_name, cfg.target_seconds,
                           r.exposure_seconds, r.relevant_failure_count,
                           r.pending_block_count, r.pending_exposure_seconds,
                           r.excluded_exposure_seconds, r.point_estimate_hours,
                           r.lower_70_hours, r.lower_90_hours, r.result_payload,
                           r.calculated_at, run.public_id AS run_public_id,
                           run.data_cutoff_at, run.implementation_version,
                           max(run.data_cutoff_at) OVER () AS all_data_cutoff_at,
                           job.status AS job_status,
                           conclusion.status AS conclusion_status,
                           conclusion.conclusion_payload,
                           conclusion.evidence_completeness,
                           conclusion.published_at
                    FROM reliability.campaign_mtbf_config cfg
                    JOIN test.test_campaign c ON c.id = cfg.campaign_id
                    JOIN reliability.mtbf_scope s ON s.id = cfg.scope_id
                    LEFT JOIN reliability.current_mtbf_result r
                      ON r.campaign_id = cfg.campaign_id AND r.scope_id = cfg.scope_id
                    LEFT JOIN reliability.calculation_run run ON run.id = r.calculation_run_id
                    LEFT JOIN reliability.current_conclusion conclusion
                      ON conclusion.population_id = r.population_id
                     AND conclusion.scope_id = r.scope_id
                    LEFT JOIN LATERAL (
                      SELECT j.status FROM reliability.recompute_job j
                      WHERE j.campaign_id = cfg.campaign_id AND j.scope_id = cfg.scope_id
                        AND j.status IN ('PENDING', 'PROCESSING')
                      ORDER BY j.id DESC LIMIT 1
                    ) job ON true
                    WHERE cfg.id > :after_id AND cfg.enabled
                      AND (CAST(:asset_kind AS text) IS NULL OR c.asset_kind = CAST(:asset_kind AS text))
                    ORDER BY cfg.id LIMIT :fetch_limit
                    """
                ),
                {
                    "after_id": after_id,
                    "asset_kind": asset_kind,
                    "fetch_limit": limit + 1,
                },
            ).mappings()
        )
        has_more = len(rows) > limit
        rows = rows[:limit]
        items: list[MtbfConclusionItem] = []
        for row in rows:
            payload = row["result_payload"] or {}
            bounds = {
                str(key): float(value)
                for key, value in (payload.get("confidence_bounds") or {}).items()
            }
            if not bounds:
                if row["lower_70_hours"] is not None:
                    bounds["0.7"] = float(row["lower_70_hours"])
                if row["lower_90_hours"] is not None:
                    bounds["0.9"] = float(row["lower_90_hours"])
            blockers: list[str] = []
            if int(row["pending_block_count"] or 0) > 0:
                blockers.append("存在未闭环 Blocked")
            if _number(row["pending_exposure_seconds"]) > 0:
                blockers.append("存在待归属暴露")
            if row["job_status"]:
                blockers.append(f"计算任务{row['job_status']}")
            if row["data_cutoff_at"] is None:
                blockers.append("尚无固定计算快照")
            items.append(
                MtbfConclusionItem(
                    campaign_id=row["campaign_public_id"],
                    campaign_code=row["campaign_code"],
                    campaign_name=row["campaign_name"],
                    asset_kind=row["asset_kind"],
                    scope=row["scope_code"],
                    scope_name=row["scope_name"],
                    target_duration_seconds=(
                        _number(row["target_seconds"])
                        if row["target_seconds"] is not None
                        else None
                    ),
                    observed=MtbfObservedSection(
                        effective_exposure_seconds=_number(row["exposure_seconds"]),
                        relevant_failure_count=int(row["relevant_failure_count"] or 0),
                        pending_failure_count=int(row["pending_block_count"] or 0),
                        pending_exposure_seconds=_number(row["pending_exposure_seconds"]),
                        excluded_seconds=_number(row["excluded_exposure_seconds"]),
                        data_cutoff_at=row["data_cutoff_at"],
                    ),
                    statistical=MtbfStatisticalSection(
                        method=row["implementation_version"] or IMPLEMENTATION_KEY,
                        point_estimate_hours=(
                            float(row["point_estimate_hours"])
                            if row["point_estimate_hours"] is not None
                            else None
                        ),
                        point_estimate_status=payload.get(
                            "point_estimate_status",
                            "NOT_CALCULATED" if row["data_cutoff_at"] is None else "NO_FINITE_ESTIMATE",
                        ),
                        lower_bounds_hours=bounds,
                        calculation_status=row["job_status"]
                        or ("CURRENT" if row["data_cutoff_at"] else "NOT_CALCULATED"),
                        run_id=row["run_public_id"],
                        calculated_at=row["calculated_at"],
                    ),
                    verified=MtbfVerifiedSection(
                        status=row["conclusion_status"] or "NOT_AVAILABLE",
                        conclusion=row["conclusion_payload"] or {},
                        evidence_completeness=row["evidence_completeness"] or {},
                        published_at=row["published_at"],
                    ),
                    blockers=blockers,
                )
            )
        return MtbfConclusionList(
            items=items,
            page=PageMeta(
                limit=limit,
                next_cursor=_encode_cursor(rows[-1]["internal_id"]) if has_more and rows else None,
            ),
            as_of_at=as_of_at,
            data_cutoff_at=rows[0]["all_data_cutoff_at"] if rows else None,
        )
