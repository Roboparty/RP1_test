"""Validated request bodies for the factory control gateway."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator


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
    def validate_mode_timing(self) -> "CanBusBinding":
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
    speed: float = Field(default=1.0, gt=0.0, le=4.0)
    loop: bool = True
    cycles: int = Field(default=0, ge=0)
    duration_hours: float | None = Field(default=None, gt=0.0)
    record: bool = True
    record_rate_hz: float = Field(default=20.0, ge=1.0, le=20.0)
    test_id: str | None = None
    robot_id: str | None = None
    execution_uuid: str | None = None
    campaign_id: str | None = None
    test_case_id: str | None = None
    test_case_version: str | None = None
    bench_id: str | None = None
    station_id: str | None = None
    operator_id: str | None = None

    @field_validator(
        "test_id",
        "robot_id",
        "execution_uuid",
        "campaign_id",
        "test_case_id",
        "test_case_version",
        "bench_id",
        "station_id",
        "operator_id",
    )
    @classmethod
    def strip_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        return value or None


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
    test_id: str = Field(
        min_length=1,
        max_length=255,
        pattern=r"^[A-Za-z0-9._-]+$",
    )
