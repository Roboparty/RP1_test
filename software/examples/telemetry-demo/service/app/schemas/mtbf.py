from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, model_validator


class MtbfPreviewRequest(BaseModel):
    exposure_hours: float = Field(ge=0)
    relevant_failure_count: int = Field(ge=0)
    confidence_levels: list[float] = Field(default_factory=lambda: [0.70, 0.90], min_length=1, max_length=10)

    @model_validator(mode="after")
    def validate_values(self) -> "MtbfPreviewRequest":
        if self.exposure_hours == 0 and self.relevant_failure_count > 0:
            raise ValueError("有相关故障时，有效暴露必须大于0。")
        if any(not 0 < value < 1 for value in self.confidence_levels):
            raise ValueError("置信度必须在0与1之间。")
        return self


class MtbfCalculationView(BaseModel):
    method: str
    exposure_hours: float
    relevant_failure_count: int
    point_estimate_hours: float | None
    point_estimate_status: str
    lower_70_hours: float
    lower_90_hours: float
    confidence_bounds: dict[str, float]
    formal: bool = False


class CampaignMtbfConfigRequest(BaseModel):
    target_hours: float | None = Field(default=1000, gt=0)
    confidence_levels: list[float] = Field(default_factory=lambda: [0.70, 0.90], min_length=1, max_length=10)
    enabled: bool = True
    settings: dict = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_confidence_levels(self) -> "CampaignMtbfConfigRequest":
        if any(not 0 < value < 1 for value in self.confidence_levels):
            raise ValueError("置信度必须在0与1之间。")
        return self


class CampaignMtbfConfigView(BaseModel):
    id: UUID
    campaign_id: UUID
    scope: str
    method: str
    target_hours: float | None
    confidence_levels: list[float]
    enabled: bool
    settings: dict
    updated_at: datetime
    etag: str


class PerAssetExposure(BaseModel):
    asset_id: UUID
    asset_code: str
    display_name: str
    included_hours: float
    excluded_hours: float
    pending_hours: float
    runtime_hours: float


class TimeIntervalDetail(BaseModel):
    interval_id: UUID
    asset_id: UUID
    asset_code: str
    status: Literal["INCLUDED", "EXCLUDED", "PENDING", "LEGACY_REPORTED"]
    started_at: datetime | None
    ended_at: datetime | None
    hours: float
    reason: str


class TimeBreakdownView(BaseModel):
    campaign_id: UUID
    scope: str
    total_test_duration_hours: float
    runtime_hours: float
    eligible_exposure_hours: float
    excluded_hours: float
    pending_hours: float
    per_asset_exposure: list[PerAssetExposure]
    intervals: list[TimeIntervalDetail]


class FailureGroupView(BaseModel):
    group_key: str
    classification: str
    review_status: str
    scope_status: str
    counted: bool
    interruption_count: int
    interruption_ids: list[UUID]
    first_seen_at: datetime
    error_codes: list[str]


class FailureBreakdownView(BaseModel):
    campaign_id: UUID
    scope: str
    relevant_failure_count: int
    pending_failure_count: int
    groups: list[FailureGroupView]


class CampaignMtbfResultView(BaseModel):
    campaign_id: UUID
    campaign_code: str
    campaign_name: str
    campaign_status: str
    asset_kind: Literal["WHOLE_MACHINE", "MODULE"]
    scope: str
    scope_name: str
    target_hours: float | None
    progress_percent: float | None
    total_test_duration_hours: float
    runtime_hours: float
    eligible_exposure_hours: float
    relevant_failure_count: int
    pending_failure_count: int
    mtbf_point_estimate_hours: float | None
    point_estimate_status: str
    mtbf_lower_70_hours: float
    mtbf_lower_90_hours: float
    confidence_bounds: dict[str, float]
    excluded_hours: float
    pending_hours: float
    per_asset_exposure: list[PerAssetExposure]
    calculation_status: str
    run_id: UUID | None
    calculated_at: datetime | None


class RecalculateRequest(BaseModel):
    reason: str = Field(default="manual recalculation", min_length=2, max_length=500)


class RecalculateResponse(BaseModel):
    job_id: UUID
    status: str
    run_id: UUID | None = None


class InterruptionUpdateRequest(BaseModel):
    classification: Literal["FAILED", "BLOCKED", "NON_RELEVANT"] | None = None
    review_status: Literal["PENDING", "CONFIRMED", "RECLASSIFIED"] | None = None
    failure_group_key: str | None = Field(default=None, max_length=200)
    relevance_reason: str | None = Field(default=None, max_length=1000)

    @model_validator(mode="after")
    def require_a_change(self) -> "InterruptionUpdateRequest":
        if not self.model_fields_set:
            raise ValueError("至少提供一个需要修改的字段。")
        return self


class InterruptionView(BaseModel):
    id: UUID
    classification: str
    review_status: str
    failure_group_key: str | None
    relevance_reason: str
    updated_at: datetime
    etag: str


class CalculationRunView(BaseModel):
    id: UUID
    campaign_id: UUID | None
    scope: str
    status: str
    data_cutoff_at: datetime
    knowledge_as_of_at: datetime
    implementation_version: str
    input_hash: str
    input_summary: dict
    output_summary: dict
    evidence_summary: dict
    error_detail: str
    created_at: datetime
    completed_at: datetime | None
