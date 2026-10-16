"""Domain models for the power-cabinet state machine.

These models deliberately contain logical names only. PLC DB numbers, offsets,
hardware IDs and protocol register addresses belong to protocol configuration.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum, IntEnum, IntFlag
from typing import Any

CHANNEL_COUNT = 4
PROTOCOL_V10 = 0x0100
PROTOCOL_V11 = 0x0101
PROTOCOL_V12 = 0x0102


class Ps1FeedbackFlags(IntFlag):
    POWER_DELAY_DONE = 1
    PORT_LOADED = 2
    STATUS_VALID = 4
    STOP_OFF_CONFIRMED = 8
    MANUAL_RESTART_LOCK = 16
    RECOVERY_EXHAUSTED = 32
    MEASUREMENT_VALID = 64
    SETPOINT_VALID = 128


class ResetResult(IntEnum):
    IDLE = 0
    PENDING = 1
    SUCCEEDED = 2
    REJECTED_NOT_SAFE = 3
    FAILED_STILL_LATCHED = 4


class FaultSource(IntFlag):
    PLC_LATCHED = 1
    PS1_COMM = 2
    PS1_DEVICE = 4
    HOST_SERVER = 8


def sequence_reached(reported: int, requested: int) -> bool:
    """Compare 16-bit transaction numbers, including 65535 -> 0 wrap."""
    return ((reported - requested) & 0xFFFF) < 0x8000


class CabinetPhase(str, Enum):
    DISCONNECTED = "DISCONNECTED"
    OFF = "OFF"
    MONITORING = "MONITORING"
    START_STOP_ACK = "START_STOP_ACK"
    START_CLEAR_STOP = "START_CLEAR_STOP"
    START_CLEAR_ACK = "START_CLEAR_ACK"
    WAIT_PLC_READY = "WAIT_PLC_READY"
    WAIT_MAIN_ACK = "WAIT_MAIN_ACK"
    WAIT_MAIN_READY = "WAIT_MAIN_READY"
    WAIT_PS1_COMM = "WAIT_PS1_COMM"
    WAIT_PS1_STOP_ACK = "WAIT_PS1_STOP_ACK"
    WAIT_PS1_STOP_READY = "WAIT_PS1_STOP_READY"
    WAIT_PS1_ACK = "WAIT_PS1_ACK"
    WAIT_PS1_OUTPUT = "WAIT_PS1_OUTPUT"
    WAIT_CHANNEL_ACK = "WAIT_CHANNEL_ACK"
    WAIT_CHANNEL_PERMIT = "WAIT_CHANNEL_PERMIT"
    RUNNING = "RUNNING"
    STOP_CHANNEL_ACK = "STOP_CHANNEL_ACK"
    WAIT_CHANNELS_OFF = "WAIT_CHANNELS_OFF"
    STOP_PS1_ACK = "STOP_PS1_ACK"
    WAIT_PS1_OFF = "WAIT_PS1_OFF"
    STOP_MAIN_ACK = "STOP_MAIN_ACK"
    WAIT_MAIN_OFF = "WAIT_MAIN_OFF"
    WAIT_RESET_RESULT = "WAIT_RESET_RESULT"
    FAULT = "FAULT"
    COMM_LOST = "COMM_LOST"
    STALE = "STALE"


class FeedbackState(str, Enum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    TIMEOUT = "timeout"
    FAILED = "failed"


@dataclass(frozen=True)
class PlcCommand:
    main_enable: bool = False
    channel_enable: tuple[bool, bool, bool, bool] = (False,) * CHANNEL_COUNT
    ps1_output_enable: bool = False
    all_stop: bool = False
    reset_fault_pulse: bool = False
    heartbeat_counter: int = 0
    command_sequence: int = 0
    set_voltage: float | None = None
    set_current: float | None = None
    apply_setpoints: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "MainEnable": self.main_enable,
            "ChannelEnable": list(self.channel_enable),
            "PS1OutputEnable": self.ps1_output_enable,
            "AllStop": self.all_stop,
            "ResetFaultPulse": self.reset_fault_pulse,
            "HeartbeatCounter": self.heartbeat_counter,
            "CommandSequence": self.command_sequence,
            "SetVoltage": self.set_voltage,
            "SetCurrent": self.set_current,
            "ApplySetpoints": self.apply_setpoints,
        }


@dataclass(frozen=True)
class PlcStatus:
    plc_ready: bool = False
    host_connected: bool = False
    # ``None`` means the active PLC protocol does not expose this signal.  It
    # must not be silently replaced with another status bit.
    main_contactor_fb: bool | None = False
    main_ready: bool = False
    physical_permit: tuple[bool | None, bool | None, bool | None, bool | None] = (
        False,
    ) * CHANNEL_COUNT
    channel_permit: tuple[bool, bool, bool, bool] = (False,) * CHANNEL_COUNT
    ps1_request: bool = False
    ps1_actual_output: bool = False
    ps1_comm_ok: bool = False
    ps1_fault: bool = False
    ps1_comm_fault: bool = False
    ps1_device_fault: bool = False
    fault_latched: bool = False
    fault_code: int = 0
    voltage: tuple[float, float, float, float] = (0.0,) * CHANNEL_COUNT
    current: tuple[float, float, float, float] = (0.0,) * CHANNEL_COUNT
    power: tuple[float, float, float, float] = (0.0,) * CHANNEL_COUNT
    analog_valid_mask: int = 0
    acknowledged_sequence: int = 0
    status_counter: int = 0
    heartbeat_echo: int = 0
    accepted_command_flags: int = 0
    protocol_magic: int = 0
    protocol_version: int = 0
    status_flags: int = 0
    reset_ack_sequence: int = 0
    reset_result: int = ResetResult.IDLE
    fault_source_flags: int = 0
    ps1_mb_status: int = 0
    ps1_device_status: int = 0
    ps1_manager_state: int = 0
    ps1_output_voltage: float = 0.0
    ps1_output_current: float = 0.0
    ps1_set_voltage: float = 0.0
    ps1_set_current: float = 0.0
    setpoint_applied: bool = False
    setpoint_rejected: bool = False
    ps1_comm_busy: bool = False
    host_server_status: int = 0
    host_server_events: int = 0
    ps1_feedback_flags: int = 0
    ps1_status_sequence: int = 0
    ps1_power_cycle: int = 0
    ps1_status_age_ms: int = 65535
    received_monotonic: float = 0.0

    @property
    def reset_results_supported(self) -> bool:
        return self.protocol_version in {PROTOCOL_V11, PROTOCOL_V12}

    @property
    def control_protocol_supported(self) -> bool:
        return self.protocol_version == PROTOCOL_V12

    @property
    def ps1_status_valid(self) -> bool:
        return (self.control_protocol_supported
                and not self.ps1_feedback_flags & 0xFF00
                and bool(self.ps1_feedback_flags & Ps1FeedbackFlags.STATUS_VALID)
                and 0 <= self.ps1_status_age_ms < 5000)

    @property
    def ps1_manual_restart_locked(self) -> bool:
        return bool(self.ps1_feedback_flags & (Ps1FeedbackFlags.MANUAL_RESTART_LOCK | Ps1FeedbackFlags.RECOVERY_EXHAUSTED))

    @property
    def effective_fault_source_flags(self) -> int:
        # Keep StatusFlags as a conservative fallback during an inconsistent
        # PLC publication or when decoding V1.0, which has no source word.
        return (self.fault_source_flags
                | (int(FaultSource.PLC_LATCHED) if self.fault_latched else 0)
                | (int(FaultSource.PS1_COMM) if self.ps1_comm_fault else 0)
                | (int(FaultSource.PS1_DEVICE) if self.ps1_device_fault else 0))

    @property
    def has_fault(self) -> bool:
        return bool(self.effective_fault_source_flags & 0x0F or self.ps1_fault)

    def to_dict(self, *, stale: bool = False, ps1_fresh: bool | None = None) -> dict[str, Any]:
        # A stale analog sample must not look like a valid live value.
        analog: list[float | None]
        currents: list[float | None]
        powers: list[float | None]
        feedback_valid = (self.ps1_status_valid and ps1_fresh is not False and not stale)
        measurement_valid = feedback_valid and bool(self.ps1_feedback_flags & Ps1FeedbackFlags.MEASUREMENT_VALID)
        setpoint_valid = feedback_valid and bool(self.ps1_feedback_flags & Ps1FeedbackFlags.SETPOINT_VALID)
        if stale or not measurement_valid:
            analog = [None] * CHANNEL_COUNT
            currents = [None] * CHANNEL_COUNT
            powers = [None] * CHANNEL_COUNT
        else:
            analog = [
                value if self.analog_valid_mask & (1 << index) else None
                for index, value in enumerate(self.voltage)
            ]
            currents = [
                value if self.analog_valid_mask & (1 << index) else None
                for index, value in enumerate(self.current)
            ]
            powers = [
                value if self.analog_valid_mask & (1 << index) else None
                for index, value in enumerate(self.power)
            ]
        return {
            "PLCReady": self.plc_ready,
            "HostConnected": self.host_connected,
            "MainContactorFB": self.main_contactor_fb,
            "MainReady": self.main_ready,
            "PhysicalPermit": list(self.physical_permit),
            "ChannelPermit": list(self.channel_permit),
            "PS1Request": self.ps1_request,
            "PS1ActualOutput": self.ps1_actual_output if feedback_valid else None,
            "PS1CommOK": self.ps1_comm_ok,
            "PS1Fault": bool(self.ps1_fault or self.effective_fault_source_flags & (FaultSource.PS1_COMM | FaultSource.PS1_DEVICE)),
            "PS1CommFault": self.ps1_comm_fault,
            "PS1DeviceFault": self.ps1_device_fault,
            "FaultLatched": self.fault_latched,
            "FaultCode": self.fault_code,
            "Voltage": analog,
            "Current": currents,
            "Power": powers,
            "AnalogValidMask": 0 if stale or not measurement_valid else self.analog_valid_mask,
            "AcknowledgedSequence": self.acknowledged_sequence,
            "StatusCounter": self.status_counter,
            "HeartbeatEcho": self.heartbeat_echo,
            "AcceptedCommandFlags": self.accepted_command_flags,
            "ProtocolMagic": self.protocol_magic,
            "ProtocolVersion": self.protocol_version,
            "StatusFlags": self.status_flags,
            "ResetAckSeq": self.reset_ack_sequence if self.reset_results_supported else None,
            "ResetResult": int(self.reset_result) if self.reset_results_supported else None,
            "ResetResultSupported": self.reset_results_supported,
            "FaultSourceFlags": self.effective_fault_source_flags,
            "RawFaultSourceFlags": self.fault_source_flags if self.reset_results_supported else None,
            "ProtocolMode": "V1.2" if self.control_protocol_supported else "V1.1 / 需升级PLC至V1.2才能控制" if self.reset_results_supported else "旧协议/无复位结果回执；需升级PLC至V1.2才能控制",
            "ControlProtocolSupported": self.control_protocol_supported,
            "PS1FeedbackFlags": self.ps1_feedback_flags if self.control_protocol_supported else None,
            "PS1StatusSeq": self.ps1_status_sequence if self.control_protocol_supported else None,
            "PS1PowerCycle": self.ps1_power_cycle if self.control_protocol_supported else None,
            "PS1StatusAgeMs": self.ps1_status_age_ms if self.control_protocol_supported else None,
            "PS1FeedbackValid": feedback_valid,
            "PS1MeasurementValid": measurement_valid and not stale,
            "PS1SetpointValid": setpoint_valid and not stale,
            "PS1StopOffConfirmed": feedback_valid and bool(self.ps1_feedback_flags & Ps1FeedbackFlags.STOP_OFF_CONFIRMED),
            "PS1ManualRestartLock": bool(self.ps1_feedback_flags & Ps1FeedbackFlags.MANUAL_RESTART_LOCK),
            "PS1RecoveryExhausted": bool(self.ps1_feedback_flags & Ps1FeedbackFlags.RECOVERY_EXHAUSTED),
            "PS1MbStatus": self.ps1_mb_status,
            "PS1DeviceStatus": self.ps1_device_status,
            "PS1ManagerState": self.ps1_manager_state,
            "PS1OutputVoltage": self.ps1_output_voltage if measurement_valid and not stale else None,
            "PS1OutputCurrent": self.ps1_output_current if measurement_valid and not stale else None,
            "PS1SetVoltage": self.ps1_set_voltage if setpoint_valid and not stale else None,
            "PS1SetCurrent": self.ps1_set_current if setpoint_valid and not stale else None,
            "SetpointApplied": self.setpoint_applied,
            "SetpointRejected": self.setpoint_rejected,
            "PS1CommBusy": self.ps1_comm_busy,
            "HostServerStatus": self.host_server_status,
            "HostServerEvents": self.host_server_events,
        }


@dataclass
class CommandFeedback:
    state: FeedbackState
    sequence: int | None = None
    detail: str = ""
    changed_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state.value,
            "sequence": self.sequence,
            "detail": self.detail,
            "changed_at": self.changed_at,
        }


@dataclass
class OperationLogEntry:
    timestamp: str
    user: str
    field: str
    old_value: Any
    new_value: Any
    command_sequence: int | None
    confirmation: FeedbackState
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["confirmation"] = self.confirmation.value
        return value
