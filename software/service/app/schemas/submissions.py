"""Bounded summaries only: no attachments, CSV, sample arrays or arbitrary JSON."""
from datetime import datetime, timedelta
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator


class TestSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, str_strip_whitespace=True)

    execution_uuid: UUID
    campaign_id: UUID
    cycle_id: UUID
    segment_id: UUID
    asset_id: UUID
    configuration_id: UUID
    test_case_version_id: UUID
    station_id: UUID
    started_at: AwareDatetime
    ended_at: AwareDatetime
    active_seconds: float = Field(ge=0, le=315360000)
    completed_cycles: int = Field(ge=0, le=2147483647)
    source_status: Literal["passed", "failed", "blocked"]
    data_quality: Literal["VALID", "PARTIAL", "INVALID", "MISSING"]
    clock_quality: Literal["VALID", "PARTIAL", "INVALID"]
    exception_count: int = Field(ge=0, le=2147483647)
    summary: str = Field(max_length=500)
    evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def valid_duration(self):
        if self.ended_at < self.started_at or self.active_seconds > (self.ended_at - self.started_at).total_seconds() + .001:
            raise ValueError("运行时长不得超过开始至结束的墙钟区间")
        if self.ended_at > datetime.now(self.ended_at.tzinfo) + timedelta(minutes=5):
            raise ValueError("结束时间不得超过当前时间五分钟")
        return self


class SubmissionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    summary: TestSummary
    expected_revision: int = Field(default=0, ge=0)
    reason: str = Field(min_length=1, max_length=300)


class SubmissionReview(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    revision: int = Field(ge=1)
    decision: Literal["APPROVED", "REJECTED"]
    reason: str = Field(min_length=1, max_length=300)
