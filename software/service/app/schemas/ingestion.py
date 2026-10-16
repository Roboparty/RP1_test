from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator


QualityStatus = Literal["VALID", "PARTIAL", "INVALID", "MISSING"]
ClockQuality = Literal["VALID", "PARTIAL", "INVALID"]
SourceStatus = Literal["running", "passed", "failed", "blocked"]
PRODUCER_EXECUTION_KEY_PATTERN = (
    r"^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,199}$"
)


class ProducerRequest(BaseModel):
    producer_key: str = Field(min_length=1, max_length=200)

    @field_validator("producer_key")
    @classmethod
    def trim_producer_key(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("producer_key is required")
        return normalized


class ExecutionRegisterRequest(ProducerRequest):
    producer_key: str = Field(
        min_length=1,
        max_length=200,
        pattern=PRODUCER_EXECUTION_KEY_PATTERN,
    )
    campaign_id: UUID
    cycle_id: UUID
    segment_id: UUID
    asset_id: UUID
    configuration_id: UUID
    test_case_version_id: UUID
    station_id: UUID
    started_at: datetime
    source_status: Literal["running"] = "running"
    stage_code: str = Field(min_length=1, max_length=100)
    stage_name: str = Field(min_length=1, max_length=255)
    source_metadata: dict[str, Any] = Field(default_factory=dict)


class ExecutionRegistration(BaseModel):
    execution_id: UUID
    execution_code: str
    stage_id: UUID
    status: Literal["RUNNING"]
    source_status: Literal["running"]
    replayed: bool = False


class HeartbeatRequest(ProducerRequest):
    occurred_at: datetime
    source_status: Literal["running", "blocked"] = "running"
    payload: dict[str, Any] = Field(default_factory=dict)
    clock_quality: ClockQuality = "VALID"
    data_quality: QualityStatus = "VALID"


class EventItem(BaseModel):
    producer_event_key: str = Field(min_length=1, max_length=200)
    event_type: str = Field(min_length=1, max_length=100)
    occurred_at: datetime
    payload: dict[str, Any] = Field(default_factory=dict)
    clock_quality: ClockQuality = "VALID"
    data_quality: QualityStatus = "VALID"


class EventBatchRequest(ProducerRequest):
    events: list[EventItem] = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def event_keys_are_unique(self) -> EventBatchRequest:
        keys = [item.producer_event_key for item in self.events]
        if len(keys) != len(set(keys)):
            raise ValueError("producer_event_key must be unique within a batch")
        return self


class TelemetryPoint(BaseModel):
    sample_index: int = Field(ge=0)
    observed_at: datetime
    value: float
    quality_status: QualityStatus = "VALID"


class TelemetrySeries(BaseModel):
    producer_series_key: str = Field(min_length=1, max_length=200)
    metric_code: str = Field(min_length=1, max_length=100)
    metric_version: str = Field(default="1.0.0", min_length=1, max_length=50)
    subject_code: str = Field(min_length=1, max_length=100)
    canonical_unit: str = Field(min_length=1, max_length=32)
    series_kind: Literal[
        "MOTION_CYCLE", "STEADY_STATE_WINDOW", "TRANSIENT", "DIAGNOSTIC"
    ]
    cycle_index: int | None = Field(default=None, ge=1)
    started_at: datetime
    ended_at: datetime
    sampling_interval_ms: float = Field(ge=10, le=3_600_000)
    downsample_method: Literal[
        "UNIFORM", "MIN_MAX", "MEAN_WINDOW", "RMS_WINDOW"
    ]
    quality_status: QualityStatus = "VALID"
    raw_data_reference: dict[str, Any] = Field(default_factory=dict)
    points: list[TelemetryPoint] = Field(min_length=1, max_length=5000)

    @model_validator(mode="after")
    def validate_series(self) -> TelemetrySeries:
        if self.ended_at < self.started_at:
            raise ValueError("ended_at must not precede started_at")
        if self.series_kind == "MOTION_CYCLE" and self.cycle_index is None:
            raise ValueError("MOTION_CYCLE requires cycle_index")
        if self.series_kind != "MOTION_CYCLE" and self.cycle_index is not None:
            raise ValueError("cycle_index is only valid for MOTION_CYCLE")
        indexes = [point.sample_index for point in self.points]
        if indexes != list(range(len(self.points))):
            raise ValueError("sample_index must be contiguous and zero-based")
        if any(
            point.observed_at < self.started_at
            or point.observed_at > self.ended_at
            for point in self.points
        ):
            raise ValueError("telemetry point lies outside series interval")
        if self.raw_data_reference.keys() & {"points", "samples", "values"}:
            raise ValueError("raw_data_reference cannot embed sample arrays")
        return self


class TelemetryBatchRequest(ProducerRequest):
    stage_id: UUID
    series: list[TelemetrySeries] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def series_keys_are_unique(self) -> TelemetryBatchRequest:
        keys = [item.producer_series_key for item in self.series]
        if len(keys) != len(set(keys)):
            raise ValueError("producer_series_key must be unique within a batch")
        return self


class ArtifactMetadataRequest(ProducerRequest):
    artifact_kind: str = Field(min_length=1, max_length=100)
    availability_status: Literal[
        "AVAILABLE", "ARCHIVED", "UNAVAILABLE_LEGACY", "MISSING"
    ]
    object_key: str | None = Field(default=None, max_length=1024)
    source_location: str = Field(default="", max_length=2048)
    file_name: str = Field(min_length=1, max_length=512)
    mime_type: str = Field(min_length=1, max_length=255)
    size_bytes: int | None = Field(default=None, ge=0)
    sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    metadata: dict[str, Any] = Field(default_factory=dict)
    trajectory_key: str | None = Field(default=None, max_length=200)
    trajectory_version: int | None = Field(default=None, ge=1)
    coordinate_frame: str = Field(default="", max_length=100)
    trajectory_point_count: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_artifact(self) -> ArtifactMetadataRequest:
        if self.availability_status == "AVAILABLE" and not self.object_key:
            raise ValueError("AVAILABLE artifact requires object_key")
        trajectory_fields = (
            self.trajectory_key,
            self.trajectory_version,
        )
        if any(value is not None for value in trajectory_fields):
            if not all(value is not None for value in trajectory_fields):
                raise ValueError(
                    "trajectory_key and trajectory_version are required together"
                )
            if self.sha256 is None:
                raise ValueError("trajectory metadata requires sha256")
        return self


class FinishExecutionRequest(ProducerRequest):
    source_status: Literal["passed", "failed", "blocked"]
    finished_at: datetime
    active_seconds: float = Field(ge=0)
    outcome: Literal["PASSED", "FAILED", "INCONCLUSIVE", "NOT_EVALUATED"]
    termination_kind: Literal[
        "NORMAL",
        "MANUAL_STOP",
        "SAFETY_WATCHDOG",
        "DATA_TIMEOUT",
        "SYSTEM_CRASH",
        "UNKNOWN",
    ]
    summary: str = Field(default="", max_length=5000)
    issues: str = Field(default="", max_length=10000)
    exception_count: int = Field(default=0, ge=0)
    data_quality: QualityStatus = "VALID"
    clock_quality: ClockQuality = "VALID"

    @model_validator(mode="after")
    def outcome_matches_source_status(self) -> FinishExecutionRequest:
        allowed = {
            "passed": {"PASSED"},
            "failed": {"FAILED"},
            "blocked": {"INCONCLUSIVE", "NOT_EVALUATED"},
        }
        if self.outcome not in allowed[self.source_status]:
            raise ValueError("outcome does not match source_status")
        return self


class IngestionResult(BaseModel):
    execution_id: UUID
    accepted_count: int = 0
    event_ids: list[UUID] = Field(default_factory=list)
    series_ids: list[UUID] = Field(default_factory=list)
    artifact_id: UUID | None = None
    trajectory_id: UUID | None = None
    runtime_interval_id: UUID | None = None
    status: str
    source_status: str | None = None
    replayed: bool = False
