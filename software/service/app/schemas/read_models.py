from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, model_validator


AssetKind = Literal["WHOLE_MACHINE", "MODULE"]
TargetPartCode = Literal["SARM", "SLEG", "SYS", "UPPER", "LOWER", "CHEST", "HEAD", "BAT"]
MetricQualityStatus = Literal["VALID", "PARTIAL", "INVALID", "MISSING"]


class PageMeta(BaseModel):
    limit: int
    next_cursor: str | None = None


class FreshnessView(BaseModel):
    as_of_at: datetime
    data_cutoff_at: datetime | None = None
    last_received_at: datetime | None = None
    lag_seconds: float | None = None
    status: Literal["FRESH", "STALE", "NO_DATA"]


class DurationTotals(BaseModel):
    total_duration_seconds: float = 0
    effective_exposure_seconds: float = 0
    excluded_seconds: float = 0
    pending_seconds: float = 0
    active_elapsed_seconds: float = 0


class HomeTestCase(BaseModel):
    id: UUID
    code: str
    name: str
    target_part_code: TargetPartCode
    target_part_name: str
    status: str
    current_elapsed_seconds: float = 0
    cumulative_duration_seconds: float = 0
    effective_exposure_seconds: float = 0
    excluded_seconds: float = 0
    pending_seconds: float = 0
    execution_count: int = 0
    scope_asset_count: int = 0
    target_duration_seconds: float | None = None
    target_progress_percent: float | None = None


class HomeScope(BaseModel):
    id: str
    code: str
    name: str
    asset_kind: AssetKind
    target_part_code: TargetPartCode
    target_part_name: str
    status: str
    part_durations: DurationTotals
    part_asset_count: int = 0
    active_execution_count: int = 0
    abnormal_asset_count: int = 0
    test_cases: list[HomeTestCase] = Field(default_factory=list)

    @model_validator(mode="after")
    def keep_test_cases_in_target_part(self) -> "HomeScope":
        if self.id != f"{self.asset_kind}:{self.target_part_code}":
            raise ValueError("首页 scope id 必须由对象类型与部位代码稳定构成。")
        if self.code != self.target_part_code:
            raise ValueError("首页 scope code 必须是部位代码，不能使用样品编号。")
        if any(item.target_part_code != self.target_part_code for item in self.test_cases):
            raise ValueError("首页部位 scope 不能混入另一适用部位的 Test Case。")
        expected = sum(item.cumulative_duration_seconds for item in self.test_cases)
        tolerance = max(1e-6, abs(expected) * 1e-9)
        if abs(self.part_durations.total_duration_seconds - expected) > tolerance:
            raise ValueError("部位累计总时长必须等于所属 Test Case 累计时长之和。")
        return self


class HomeObjectGroup(BaseModel):
    asset_kind: AssetKind
    objects: list[HomeScope] = Field(default_factory=list)

    @model_validator(mode="after")
    def keep_asset_kinds_isolated(self) -> "HomeObjectGroup":
        if any(item.asset_kind != self.asset_kind for item in self.objects):
            raise ValueError("首页对象组不能混入另一种 asset_kind。")
        return self


class SourceHealthItem(BaseModel):
    id: UUID
    code: str
    source_type: str
    configured_status: str
    last_received_at: datetime | None = None
    last_receipt_status: Literal["ACCEPTED", "DUPLICATE", "REJECTED", "PARTIAL"] | None = None
    last_success_at: datetime | None = None
    lag_seconds: float | None = None
    freshness_status: Literal[
        "FRESH", "STALE", "NO_DATA", "DISABLED", "DEGRADED", "ERROR"
    ]
    last_error: str | None = None


class DataSourceHealthView(BaseModel):
    as_of_at: datetime
    data_cutoff_at: datetime | None = None
    sources: list[SourceHealthItem] = Field(default_factory=list)
    healthy_count: int = 0
    warning_count: int = 0


class DashboardHome(BaseModel):
    freshness: FreshnessView
    data_sources: DataSourceHealthView
    anomaly_count: int = 0
    anomalies: list[str] = Field(default_factory=list)
    groups: list[HomeObjectGroup]


class TestCaseDurationItem(BaseModel):
    id: UUID
    code: str
    name: str
    asset_kind: AssetKind
    asset_id: UUID
    asset_code: str
    asset_name: str
    status: str
    execution_count: int
    durations: DurationTotals
    data_cutoff_at: datetime | None = None


class TestCaseDurationList(BaseModel):
    items: list[TestCaseDurationItem]
    page: PageMeta
    totals: DurationTotals
    as_of_at: datetime
    data_cutoff_at: datetime | None = None


class AssetSummary(BaseModel):
    id: UUID
    code: str
    name: str
    asset_kind: AssetKind
    target_part_code: TargetPartCode | None = None
    target_part_name: str | None = None
    product_family: str
    batch_code: str | None = None
    serial_number: str
    lifecycle_status: str
    current_test_case_code: str | None = None
    current_execution_id: UUID | None = None
    current_execution_code: str | None = None
    health_status: str | None = None
    durations: DurationTotals
    freshness: FreshnessView


class AssetList(BaseModel):
    items: list[AssetSummary]
    page: PageMeta
    as_of_at: datetime
    data_cutoff_at: datetime | None = None


class ConfigurationView(BaseModel):
    id: UUID | None = None
    fingerprint: str | None = None
    effective_from: datetime | None = None
    reliability_impact: str | None = None
    hardware_manifest: dict[str, Any] = Field(default_factory=dict)
    software_manifest: dict[str, Any] = Field(default_factory=dict)
    parameter_manifest: dict[str, Any] = Field(default_factory=dict)


class CurrentContextView(BaseModel):
    campaign_code: str | None = None
    cycle_code: str | None = None
    execution_id: UUID | None = None
    execution_code: str | None = None
    test_case_code: str | None = None
    stage_code: str | None = None
    stage_name: str | None = None
    status: str | None = None
    started_at: datetime | None = None
    current_elapsed_seconds: float = 0
    is_stale: bool = False


class EvidenceSummary(BaseModel):
    total_count: int = 0
    available_count: int = 0
    missing_count: int = 0


class AssetDetail(BaseModel):
    asset: AssetSummary
    current_context: CurrentContextView
    configuration: ConfigurationView
    execution_count: int = 0
    event_count: int = 0
    health_summary: dict[str, int] = Field(default_factory=dict)
    evidence_summary: EvidenceSummary
    as_of_at: datetime
    data_cutoff_at: datetime | None = None


class PerformanceTrendPoint(BaseModel):
    observed_at: datetime
    elapsed_seconds: float
    progress_percent: float
    value: float
    quality_status: str


class PerformanceTrendSeries(BaseModel):
    stage_code: str
    stage_name: str
    sequence_no: int
    status: str
    points: list[PerformanceTrendPoint] = Field(default_factory=list)
    min_value: float
    max_value: float
    avg_value: float


class PerformanceTrendMetric(BaseModel):
    metric_code: str
    metric_name: str
    unit: str
    series: list[PerformanceTrendSeries] = Field(default_factory=list)


class AssetPerformanceTrends(BaseModel):
    asset_id: UUID
    asset_code: str
    asset_name: str
    execution_id: UUID | None = None
    execution_code: str | None = None
    test_case_code: str | None = None
    metrics: list[PerformanceTrendMetric]
    as_of_at: datetime
    data_cutoff_at: datetime | None = None


class ExecutionListItem(BaseModel):
    id: UUID
    code: str
    asset_id: UUID
    asset_code: str
    test_case_id: UUID
    test_case_code: str
    test_case_name: str
    campaign_code: str
    cycle_code: str
    stage_count: int = 0
    status: str
    started_at: datetime | None = None
    ended_at: datetime | None = None
    closed_duration_seconds: float = 0
    active_elapsed_seconds: float = 0
    data_quality: str
    clock_quality: str


class ExecutionList(BaseModel):
    items: list[ExecutionListItem]
    page: PageMeta
    as_of_at: datetime
    data_cutoff_at: datetime | None = None


class EventListItem(BaseModel):
    id: UUID
    event_type: str
    normalized_time: datetime
    execution_id: UUID | None = None
    data_quality: str
    clock_quality: str
    payload: dict[str, Any] = Field(default_factory=dict)
    interruption_classification: str | None = None
    interruption_review_status: str | None = None


class EventList(BaseModel):
    items: list[EventListItem]
    page: PageMeta
    as_of_at: datetime
    data_cutoff_at: datetime | None = None


class MtbfObservationView(BaseModel):
    scope: str
    method: str | None = None
    data_cutoff_at: datetime | None = None
    effective_exposure_seconds: float = 0
    relevant_failure_count: int = 0
    pending_failure_count: int = 0
    pending_exposure_seconds: float = 0
    point_estimate_hours: float | None = None
    point_estimate_status: str = "NO_EXPOSURE"
    lower_bounds_hours: dict[str, float] = Field(default_factory=dict)


class TestCaseDetail(BaseModel):
    id: UUID
    code: str
    name: str
    asset_kind: AssetKind
    target_part_code: TargetPartCode | None = None
    target_part_name: str | None = None
    domain: str
    evidence_type: str
    enabled: bool
    latest_version: str | None = None
    status_counts: dict[str, int] = Field(default_factory=dict)
    durations: DurationTotals
    mtbf_observation: MtbfObservationView | None = None
    executions: ExecutionList
    as_of_at: datetime
    data_cutoff_at: datetime | None = None


class MetricObservationView(BaseModel):
    id: UUID
    metric_code: str
    metric_name: str
    joint: str | None = None
    value: float | None = None
    text_value: str | None = None
    unit: str | None = None
    observed_at: datetime
    quality_status: str
    formal_eligible: bool


class MetricPeakView(BaseModel):
    metric_code: str
    metric_name: str
    value: float
    unit: str | None = None
    observed_at: datetime


class ArtifactView(BaseModel):
    id: UUID
    kind: str
    file_name: str
    mime_type: str
    size_bytes: int | None = None
    availability_status: str
    sha256: str | None = None


class ExecutionResultView(BaseModel):
    source_status: Literal["passed", "failed", "blocked", "scheduled", "running"]
    outcome: Literal["PASSED", "FAILED", "INCONCLUSIVE", "NOT_EVALUATED"]
    termination_kind: Literal[
        "NORMAL",
        "MANUAL_STOP",
        "SAFETY_WATCHDOG",
        "DATA_TIMEOUT",
        "SYSTEM_CRASH",
        "SCHEDULED",
        "RUNNING",
        "UNKNOWN",
    ]
    summary: str
    issues: str
    exception_count: int = Field(ge=0)
    reported_duration_seconds: float = Field(ge=0)
    executor_display: str
    environment_label: str
    last_data_at: datetime | None = None
    telemetry_source: str
    archive_status: Literal["PENDING", "IMPORTED", "ARCHIVED", "MISSING"]
    archive_path: str
    report_reference: str
    normalization_flags: list[dict[str, Any]] = Field(default_factory=list)


class JointAnalysisSubject(BaseModel):
    subject_code: str
    name: str
    subject_kind: str
    target_part_code: str | None = None
    display_order: int = Field(ge=0)
    enabled: bool


class JointAnalysisStage(BaseModel):
    id: UUID
    stage_code: str
    stage_name: str
    sequence_no: int = Field(ge=1)
    status: Literal[
        "SCHEDULED", "RUNNING", "PAUSED", "COMPLETED",
        "FAILED", "BLOCKED", "CANCELLED",
    ]


class JointAnalysisCycle(BaseModel):
    cycle_index: int = Field(ge=0)
    started_at: datetime | None = None
    ended_at: datetime | None = None
    quality_status: MetricQualityStatus
    series_count: int = Field(ge=0)


class JointAnalysisMetric(BaseModel):
    metric_code: str
    metric_name: str
    unit: str


class JointAnalysisSelection(BaseModel):
    subject_code: str | None = None
    stage_code: str | None = None
    cycle_index: int | None = None


class JointAnalysisSummaryItem(BaseModel):
    code: str
    label: str
    source_metric_code: str | None = None
    value: float | None = None
    unit: str
    sample_count: int = Field(default=0, ge=0)
    quality_status: Literal["VALID", "PARTIAL", "NO_DATA", "NOT_CALCULABLE"]


class JointAnalysisPoint(BaseModel):
    sample_index: int = Field(ge=0)
    elapsed_seconds: float = Field(ge=0)
    progress_percent: float | None = Field(default=None, ge=0, le=100)
    value: float | None = None
    observed_at: datetime
    quality_status: MetricQualityStatus


class JointAnalysisSeries(BaseModel):
    id: UUID
    metric_code: str
    metric_name: str
    unit: str
    subject_code: str
    stage_code: str
    series_kind: str
    cycle_index: int = Field(ge=0)
    point_count: int = Field(ge=0)
    display_point_count: int = Field(ge=0, le=120)
    started_at: datetime | None = None
    ended_at: datetime | None = None
    sampling_interval_ms: int | None = Field(default=None, ge=1)
    downsample_method: str | None = None
    quality_status: MetricQualityStatus
    points: list[JointAnalysisPoint] = Field(default_factory=list)


class ExecutionJointAnalysis(BaseModel):
    execution_id: UUID
    execution_code: str
    subjects: list[JointAnalysisSubject]
    stages: list[JointAnalysisStage]
    cycles: list[JointAnalysisCycle]
    metrics: list[JointAnalysisMetric]
    resolved_selection: JointAnalysisSelection
    summary: list[JointAnalysisSummaryItem]
    series: list[JointAnalysisSeries]
    as_of_at: datetime
    data_cutoff_at: datetime | None


class ExecutionDetail(BaseModel):
    execution: ExecutionListItem
    configuration: ConfigurationView
    station_code: str | None = None
    station_name: str | None = None
    operator_name: str | None = None
    source_code: str | None = None
    result: ExecutionResultView | None = None
    peak_summary: list[MetricPeakView] = Field(default_factory=list)
    metrics: list[MetricObservationView] = Field(default_factory=list)
    data_quality_summary: dict[str, Any] = Field(default_factory=dict)
    evidence: list[ArtifactView] = Field(default_factory=list)
    as_of_at: datetime
    data_cutoff_at: datetime | None = None


class MtbfObservedSection(BaseModel):
    effective_exposure_seconds: float
    relevant_failure_count: int
    pending_failure_count: int
    pending_exposure_seconds: float
    excluded_seconds: float
    data_cutoff_at: datetime | None = None


class MtbfStatisticalSection(BaseModel):
    method: str
    point_estimate_hours: float | None
    point_estimate_status: str
    lower_bounds_hours: dict[str, float]
    calculation_status: str
    run_id: UUID | None = None
    calculated_at: datetime | None = None


class MtbfVerifiedSection(BaseModel):
    status: str
    conclusion: dict[str, Any] = Field(default_factory=dict)
    evidence_completeness: dict[str, Any] = Field(default_factory=dict)
    published_at: datetime | None = None


class MtbfConclusionItem(BaseModel):
    campaign_id: UUID
    campaign_code: str
    campaign_name: str
    asset_kind: AssetKind
    scope: str
    scope_name: str
    target_duration_seconds: float | None = None
    observed: MtbfObservedSection
    statistical: MtbfStatisticalSection
    verified: MtbfVerifiedSection
    blockers: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def enforce_zero_failure_semantics(self) -> "MtbfConclusionItem":
        if (
            self.observed.relevant_failure_count == 0
            and self.statistical.point_estimate_hours is not None
        ):
            raise ValueError("零故障时不得返回有限或无限 MTBF 点估计。")
        return self


class MtbfConclusionList(BaseModel):
    items: list[MtbfConclusionItem]
    page: PageMeta
    as_of_at: datetime
    data_cutoff_at: datetime | None = None
