"""Validated request bodies for the factory control gateway."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class MotorOverride(BaseModel):
    index: int = Field(ge=0)
    motor_id: int = Field(ge=0)
    motor_type: str = Field(min_length=1)
    motor_model: int = Field(ge=0)
    zero_offset: float
    kp: float = Field(ge=0.0)
    kd: float = Field(ge=0.0)
    sign: float

    @field_validator("motor_type")
    @classmethod
    def normalize_motor_type(cls, value: str) -> str:
        return value.strip().upper()


class CanBusBinding(BaseModel):
    index: int = Field(ge=0)
    interface: str = Field(pattern=r"^can[0-9]+$")
    mode: Literal["can", "canfd"] = "canfd"
    bitrate: int
    dbitrate: int | None = None

    @field_validator("bitrate")
    @classmethod
    def allowed_bitrate(cls, value: int) -> int:
        if value not in {125_000, 250_000, 500_000, 1_000_000}:
            raise ValueError("unsupported CAN arbitration bitrate")
        return value

    @model_validator(mode="after")
    def validate_mode_timing(self) -> CanBusBinding:
        if self.mode == "can":
            if self.dbitrate is not None:
                raise ValueError("classic CAN must not define dbitrate")
            return self
        if self.dbitrate not in {1_000_000, 2_000_000, 4_000_000, 5_000_000}:
            raise ValueError("unsupported CAN-FD data bitrate")
        return self


class ConfigureRequest(BaseModel):
    config_path: str = Field(min_length=1)
    limb: str = Field(min_length=1)
    backend: Literal["motors_py", "fake"] = "motors_py"
    bus_bindings: list[CanBusBinding] | None = None
    motor_overrides: list[MotorOverride] | None = None


class ConfigPreviewRequest(BaseModel):
    config_path: str = Field(min_length=1)
    limb: str = Field(min_length=1)


class CanConnectRequest(BaseModel):
    bus_bindings: list[CanBusBinding] | None = None


class TrajectoryImportRequest(BaseModel):
    path: str = Field(min_length=1)


class EnableMotorsRequest(BaseModel):
    physical_estop_confirmed: bool


class PlaybackStartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    speed: float = Field(default=1.0, gt=0.0, le=4.0)
    loop: bool = True
    cycles: int = Field(default=0, ge=0)
    duration_hours: float | None = Field(default=None, gt=0.0)
    record: bool = True
    recording_scope: Literal["local", "platform"] | None = None
    record_rate_hz: float = Field(default=20.0, ge=1.0, le=20.0)
    test_id: str | None = None
    robot_id: str | None = None
    execution_uuid: str | None = None
    campaign_id: str | None = None
    cycle_id: str | None = None
    segment_id: str | None = None
    asset_id: str | None = None
    configuration_id: str | None = None
    test_case_version_id: str | None = None
    test_case_id: str | None = None
    test_case_version: str | None = None
    test_name: str | None = Field(default=None, max_length=200)
    bench_id: str | None = None
    station_id: str | None = None
    operator_id: str | None = None
    stage_code: str | None = None
    stage_name: str | None = None
    subject_map: dict[str, str] = Field(default_factory=dict)

    @field_validator(
        "test_id",
        "robot_id",
        "execution_uuid",
        "campaign_id",
        "cycle_id",
        "segment_id",
        "asset_id",
        "configuration_id",
        "test_case_version_id",
        "test_case_id",
        "test_case_version",
        "bench_id",
        "station_id",
        "operator_id",
        "stage_code",
        "stage_name",
    )
    @classmethod
    def strip_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        return value or None

    @field_validator("subject_map")
    @classmethod
    def normalize_subject_map(cls, value: dict[str, str]) -> dict[str, str]:
        return {
            str(joint).strip(): str(subject).strip()
            for joint, subject in value.items()
            if str(joint).strip() and str(subject).strip()
        }

    @model_validator(mode="after")
    def require_test_identity(self) -> PlaybackStartRequest:
        if not str(self.test_case_id or "").strip():
            raise ValueError("测试用例编号必填，没有对应用例请选择 OTHER")
        if self.test_case_id.upper() == "OTHER":
            if not str(self.test_name or "").strip():
                raise ValueError("其他用例必须填写临时测试名称")
            if not str(self.robot_id or "").strip() or not str(self.bench_id or "").strip():
                raise ValueError("其他用例必须填写实际测试对象和工位")
        return self


class ManualMoveRequest(BaseModel):
    targets_deg: list[float] = Field(min_length=1, max_length=32)
    speed_deg_s: float = Field(gt=0.0, le=360.0)


class ZeroPrepareRequest(BaseModel):
    motor_index: int = Field(ge=0)


class ZeroMotorRequest(BaseModel):
    motor_index: int = Field(ge=0)


class ZeroConfirmRequest(BaseModel):
    allow_on_fault: bool = False


class PlatformLoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=255)
    password: str = Field(min_length=1, max_length=4096)
    station_id: str | None = Field(
        default=None,
        max_length=255,
        pattern=r"^(?:[A-Za-z0-9._-]+)?$",
    )


class ExecutionActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    test_id: str = Field(
        min_length=1,
        max_length=255,
        pattern=r"^[A-Za-z0-9._-]+$",
    )
    reason: str = Field(default="", max_length=300)
    verified_status: str = Field(default="", max_length=16)
    operator_summary: str = Field(default="", max_length=500)
    tester: str = Field(default="", max_length=80)
    bench: str = Field(default="", max_length=64)
    outcome: str = Field(default="", max_length=8)
    notes: str = Field(default="", max_length=300)
    analysis: str = Field(default="", max_length=500)
    station_id: str = Field(default="", max_length=64)
    discrepancy_reason: str = Field(default="", max_length=300)
    test_name: str = Field(default="", max_length=200)
    actual_object: str = Field(default="", max_length=200)


class HistoryImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    paths: list[str] = Field(min_length=1, max_length=100)
    test_name: str = Field(min_length=1, max_length=200)
    actual_object: str = Field(min_length=1, max_length=200)
    bench_id: str = Field(min_length=1, max_length=64)
    operator_id: str = Field(min_length=1, max_length=80)


class BenchCodeRequest(BaseModel):
    bench_code: str = Field(
        default="",
        max_length=64,
        pattern=r"^(?:[A-Za-z0-9][A-Za-z0-9._-]*)?$",
    )


class PlcOperatorRequest(BaseModel):
    request_id: str | None = Field(default=None, min_length=1, max_length=128)
    expected_epoch: str | None = Field(default=None, min_length=1, max_length=128)
    expected_revision: int | None = Field(default=None, ge=0)
    user: str = Field(default="local-operator", min_length=1, max_length=128)

    @field_validator("user")
    @classmethod
    def normalize_user(cls, value: str) -> str:
        return value.strip()


class PlcStartRequest(PlcOperatorRequest):
    channels: list[int] = Field(default_factory=lambda: [1, 2, 3, 4])
    voltage: float | None = Field(default=None, gt=0.0, le=60.0)
    current: float | None = Field(default=None, gt=0.0, le=60.0)

    @field_validator("channels")
    @classmethod
    def validate_channels(cls, value: list[int]) -> list[int]:
        if any(channel not in {1, 2, 3, 4} for channel in value):
            raise ValueError("PLC channels must be in [1, 4]")
        if len(value) != len(set(value)):
            raise ValueError("PLC channels must be unique")
        return sorted(value)

    @model_validator(mode="after")
    def validate_setpoint_pair(self) -> PlcStartRequest:
        if (self.voltage is None) != (self.current is None):
            raise ValueError("voltage and current must be provided together")
        return self


class PlcChannelRequest(PlcOperatorRequest):
    enabled: bool


class PlcSetpointRequest(PlcOperatorRequest):
    voltage: float = Field(ge=0.0, le=1000.0)
    current: float = Field(ge=0.0, le=1000.0)
