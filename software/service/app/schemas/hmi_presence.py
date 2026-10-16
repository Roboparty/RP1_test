from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class HmiRun(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    session_key: str = Field(min_length=1, max_length=128)
    state: Literal["idle", "armed", "running", "paused", "stopping", "fault", "completed", "unknown"]
    test_id: str = Field(default="", max_length=255)
    execution_code: str = Field(default="", max_length=255)
    execution_uuid: UUID | None = None
    case_code: str = Field(default="", max_length=128)
    sample_id: str = Field(default="", max_length=128)
    test_case: str = Field(default="", max_length=128)
    active_seconds: float = Field(default=0, ge=0, le=315360000)
    completed_cycles: int = Field(default=0, ge=0, le=2147483647)
    campaign_id: str = Field(default="", max_length=36)
    cycle_id: str = Field(default="", max_length=36)
    asset_id: str = Field(default="", max_length=36)
    configuration_id: str = Field(default="", max_length=36)
    test_case_version_id: str = Field(default="", max_length=36)
    station_id: str = Field(default="", max_length=36)


class HmiPlcStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")
    driver: str = Field(default="unknown", max_length=32)
    communication_state: str = Field(default="DISCONNECTED", max_length=32)
    phase: str = Field(default="DISCONNECTED", max_length=64)
    powered_channels: list[Annotated[int, Field(ge=1, le=4)]] = Field(default_factory=list, max_length=4)
    stale: bool = True


class HmiHeartbeat(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    installation_id: UUID
    station_name: str = Field(min_length=1, max_length=128)
    bench_id: str = Field(default="", max_length=64)
    sequence: int = Field(ge=1, le=9223372036854775807)
    snapshot_age_seconds: float = Field(ge=0, le=86400)
    runs: list[HmiRun] = Field(default_factory=list, max_length=32)
    plc: HmiPlcStatus = Field(default_factory=HmiPlcStatus)


class HmiStationView(BaseModel):
    connection_id: UUID
    installation_id: UUID
    station_name: str
    bench_id: str
    operator_name: str
    operator_display_name: str
    online: bool
    snapshot_fresh: bool
    state: Literal["offline", "unknown", "running", "paused", "fault", "powered", "idle"]
    connected_at: datetime
    last_seen_at: datetime
    snapshot_at: datetime
    runs: list[HmiRun]
    plc: HmiPlcStatus


class HmiStationList(BaseModel):
    items: list[HmiStationView]
    next_cursor: UUID | None = None
    as_of_at: datetime
    offline_after_seconds: int = 45
    snapshot_stale_after_seconds: int = 20
