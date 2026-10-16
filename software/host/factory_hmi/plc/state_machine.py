"""Safety-oriented power sequencing independent of the PLC protocol."""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any

from .client import PlcClient, PlcCommunicationError
from .client import PlcConfigurationError
from .config import PlcSetpointConfig, PlcTimingConfig
from .freshness import Ps1FeedbackTracker
from .faults import cabinet_fault_messages
from .models import (
    CabinetPhase,
    CommandFeedback,
    FeedbackState,
    OperationLogEntry,
    PlcCommand,
    PlcStatus,
    ResetResult,
    Ps1FeedbackFlags,
    sequence_reached,
)


@dataclass
class _PendingCommand:
    sequence: int
    deadline: float
    next_phase: CabinetPhase
    controls: tuple[str, ...]
    log_indexes: tuple[int, ...]
    setpoint_rejection_preexisting: bool = False
    setpoint_rejection_cleared: bool = False


class PlcCabinetController:
    """Deterministic state machine for logical PLC commands.

    The controller never knows addresses or transports. Every changed control
    is a sequenced transaction and remains pending until the PLC reports an
    equal or newer ``AcknowledgedSequence``.
    """

    def __init__(
        self,
        client: PlcClient,
        timings: PlcTimingConfig | None = None,
        setpoints: PlcSetpointConfig | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.client = client
        self.timings = timings or PlcTimingConfig()
        self.setpoints = setpoints or PlcSetpointConfig()
        self.clock = clock
        self.phase = CabinetPhase.DISCONNECTED
        self.command = PlcCommand()
        self.status: PlcStatus | None = None
        self.communication_state = "DISCONNECTED"
        self.last_error = ""
        self._pending: _PendingCommand | None = None
        self._phase_deadline = 0.0
        self._requested_channels = (False, False, False, False)
        self._start_setpoints: tuple[float, float] | None = None
        self._start_requested = False
        self._ever_connected = False
        self._ps1_feedback = Ps1FeedbackTracker()
        self._startup_deadline = 0.0
        self.feedback: dict[str, CommandFeedback] = {
            name: CommandFeedback(FeedbackState.CONFIRMED, detail="not requested")
            for name in (
                "Start",
                "Stop",
                "MainEnable",
                "PS1OutputEnable",
                "ChannelEnable1",
                "ChannelEnable2",
                "ChannelEnable3",
                "ChannelEnable4",
                "AllStop",
                "ResetFaultPulse",
                "Setpoints",
            )
        }
        self.operation_log: list[OperationLogEntry] = []

    @staticmethod
    def _timestamp() -> str:
        return datetime.now(timezone.utc).isoformat()

    @property
    def stale(self) -> bool:
        if self.status is None:
            return True
        return (
            self.clock() - self.status.received_monotonic
            > self.timings.stale_timeout_ms / 1000.0
        )

    def connect(self, *, user: str = "system") -> None:
        try:
            self.client.connect()
            self.status = self.client.read_status()
            if not self.status.control_protocol_supported:
                raise PlcConfigurationError("控制需配套PLC V1.5 / Host Protocol V1.2（0x0102）；旧协议仅可监控，请升级配套源码")
            self._ps1_feedback.reset()
            self._ps1_feedback.observe(self.status, self.clock())
            # Start from the PLC's accepted transaction and heartbeat echo.
            # The first write keeps the current CommandSeq, so the protocol
            # adapter sends only ControlKey/Heartbeat and cannot energize an
            # output before HostConnected is confirmed.
            self.command = PlcCommand(
                heartbeat_counter=(self.status.heartbeat_echo + 1) & 0xFFFF,
                command_sequence=self.status.acknowledged_sequence,
                set_voltage=self.status.ps1_set_voltage,
                set_current=self.status.ps1_set_current,
            )
            self.communication_state = "CONNECTING"
            self._pending = None
            self._start_requested = False
            self._start_setpoints = None
            self._requested_channels = (False, False, False, False)
            self.phase = CabinetPhase.DISCONNECTED
            self._establish_host_connection()

            self.communication_state = "LIVE"
            self._ever_connected = True
            self.phase = CabinetPhase.OFF

            # Establish an explicit, acknowledged STOP before clearing it with
            # every enable still OFF.  This satisfies the PLC setpoint guard
            # and guarantees that reconnect never restores a previous run.
            self._issue(
                user=user,
                phase=CabinetPhase.OFF,
                changes={
                    "main_enable": False,
                    "ps1_output_enable": False,
                    "channel_enable": (False, False, False, False),
                    "all_stop": True,
                },
                force=True,
                detail="connection safety initialization: acknowledged AllStop",
            )
            self._wait_for_pending_confirmation("initial AllStop")
            self._issue(
                user=user,
                phase=CabinetPhase.OFF,
                changes={
                    "main_enable": False,
                    "ps1_output_enable": False,
                    "channel_enable": (False, False, False, False),
                    "all_stop": False,
                },
                force=True,
                detail="connection initialized with every output request OFF",
            )
            self._wait_for_pending_confirmation("safe output initialization")
            self.phase = CabinetPhase.OFF
        except Exception:
            with suppress(Exception):
                self.client.disconnect()
            self.communication_state = "COMM_LOST"
            self.phase = CabinetPhase.COMM_LOST
            self.status = None
            self._pending = None
            self.command = PlcCommand()
            raise

    def _cancel_sequence_feedback(self, detail: str) -> None:
        for control in ("Start", "Stop"):
            if self.feedback[control].state == FeedbackState.PENDING:
                self.feedback[control] = CommandFeedback(
                    FeedbackState.REJECTED, self.command.command_sequence, detail,
                )

    def disconnect(self) -> None:
        self._cancel_sequence_feedback("PLC disconnected")
        self.client.disconnect()
        self.communication_state = "DISCONNECTED"
        self.phase = CabinetPhase.DISCONNECTED
        self.status = None
        self._pending = None
        self._start_requested = False
        self._start_setpoints = None
        self._requested_channels = (False, False, False, False)
        self._ps1_feedback.reset()
        self.command = PlcCommand(command_sequence=self.command.command_sequence)

    def _establish_host_connection(self) -> None:
        poll_seconds = self.timings.poll_ms / 1000.0
        attempts = max(
            1, math.ceil(self.timings.command_timeout_ms / self.timings.poll_ms)
        )
        heartbeat_every = max(
            1, math.ceil(self.timings.heartbeat_ms / self.timings.poll_ms)
        )
        self.client.write_command(self.command)
        for attempt in range(attempts):
            self.status = self.client.read_status()
            if self.status.host_connected and self.status.heartbeat_echo == (
                self.command.heartbeat_counter & 0xFFFF
            ):
                return
            if (attempt + 1) % heartbeat_every == 0:
                self.command = replace(
                    self.command,
                    heartbeat_counter=(self.command.heartbeat_counter + 1) & 0xFFFFFFFF,
                )
                self.client.write_command(self.command)
            if attempt + 1 < attempts:
                time.sleep(poll_seconds)
        raise PlcCommunicationError(
            "PLC did not confirm HostConnected and the new heartbeat before timeout"
        )

    def _wait_for_pending_confirmation(self, operation: str) -> None:
        poll_seconds = self.timings.poll_ms / 1000.0
        attempts = max(
            1, math.ceil(self.timings.command_timeout_ms / self.timings.poll_ms)
        )
        heartbeat_every = max(
            1, math.ceil(self.timings.heartbeat_ms / self.timings.poll_ms)
        )
        for attempt in range(attempts):
            self._read_and_resolve(self.clock())
            if self._pending is None:
                return
            if (attempt + 1) % heartbeat_every == 0:
                self.command = replace(
                    self.command,
                    heartbeat_counter=(self.command.heartbeat_counter + 1) & 0xFFFFFFFF,
                )
                self.client.write_command(self.command)
            if attempt + 1 < attempts:
                time.sleep(poll_seconds)
        if self._pending is not None:
            self._finish_pending(
                FeedbackState.TIMEOUT, f"{operation} acknowledgement timeout"
            )
        raise PlcCommunicationError(f"{operation} acknowledgement timeout")

    def request_start(
        self,
        channels: tuple[bool, bool, bool, bool] = (True, True, True, True),
        *,
        user: str,
        voltage: float | None = None,
        current: float | None = None,
    ) -> bool:
        if len(channels) != 4:
            raise ValueError("exactly four channel selections are required")
        if (
            not self.client.connected
            or self.communication_state != "LIVE"
            or self.stale
        ):
            return self._reject("Start", user, "PLC communication is not live")
        if self._pending is not None:
            return self._reject("Start", user, "another command is awaiting PLC acknowledgement")
        if self.phase != CabinetPhase.OFF:
            return self._reject("Start", user, f"cannot start from {self.phase.value}")
        if self.status is None or not self.status.host_connected:
            return self._reject("Start", user, "PLC has not confirmed HostConnected")
        if not self.status.control_protocol_supported:
            return self._reject("Start", user, "控制需配套PLC V1.5 / Host Protocol V1.2（0x0102）")
        if self.status.ps1_manual_restart_locked or self.status.ps1_feedback_flags & 0xFF00:
            return self._reject("Start", user, "PLC人工重启锁/恢复耗尽或反馈标志非法；请检查PLC诊断")
        if self.status and self.status.has_fault:
            return self._reject("Start", user, "PLC or PS1 fault is active")
        if (voltage is None) != (current is None):
            return self._reject(
                "Start",
                user,
                "voltage and current must be provided together",
            )
        try:
            start_setpoints = (
                self._validated_setpoints(float(voltage), float(current))
                if voltage is not None and current is not None
                else None
            )
        except ValueError as exc:
            return self._reject("Start", user, str(exc))
        self._requested_channels = tuple(bool(value) for value in channels)  # type: ignore[assignment]
        self._start_setpoints = start_setpoints
        self._start_requested = True
        self._startup_deadline = self.clock() + self.startup_budget_ms / 1000.0
        self.feedback["Start"] = CommandFeedback(
            FeedbackState.PENDING,
            detail="re-arming acknowledged STOP before startup",
        )
        self._issue(
            user=user,
            phase=CabinetPhase.START_STOP_ACK,
            changes={
                "main_enable": False,
                "ps1_output_enable": False,
                "channel_enable": (False, False, False, False),
                "all_stop": True,
            },
            force=True,
            detail="startup safety step 1/2: acknowledged AllStop",
        )
        return True

    def request_stop(self, *, user: str) -> bool:
        if (
            not self.client.connected
            or self.communication_state != "LIVE"
            or self.stale
        ):
            return self._reject("Stop", user, "PLC communication is not live")
        if self._pending is not None:
            return self._reject(
                "Stop",
                user,
                "another command is awaiting PLC acknowledgement; use AllStop for immediate stop",
            )
        self._start_requested = False
        self._start_setpoints = None
        self._requested_channels = (False, False, False, False)
        self.feedback["Stop"] = CommandFeedback(
            FeedbackState.PENDING,
            detail="stopping channels before PS1 and main contactor",
        )
        self._begin_stop(user)
        return True

    def request_channel(self, channel: int, enabled: bool, *, user: str) -> bool:
        if channel not in {1, 2, 3, 4}:
            raise ValueError("channel must be in [1, 4]")
        control = f"ChannelEnable{channel}"
        if (
            not self.client.connected
            or self.communication_state != "LIVE"
            or self.stale
        ):
            return self._reject(control, user, "PLC communication is not live")
        if self._pending is not None:
            return self._reject(
                control, user, "another command is awaiting PLC acknowledgement"
            )
        if self.phase != CabinetPhase.RUNNING:
            return self._reject(control, user, "channel changes require RUNNING phase")
        if enabled:
            reason = self._output_admission_reason()
            if reason:
                return self._reject(control, user, reason)
            if self.phase != CabinetPhase.RUNNING:
                return self._reject(
                    control, user, "channel enable requires RUNNING phase"
                )
            assert self.status is not None
            if not self.status.ps1_actual_output:
                return self._reject(control, user, "PS1 actual output is OFF")
            if self.status.physical_permit[channel - 1] is False:
                return self._reject(control, user, "physical permit is absent")
        values = list(self.command.channel_enable)
        values[channel - 1] = bool(enabled)
        requested = list(self._requested_channels)
        requested[channel - 1] = bool(enabled)
        self._requested_channels = tuple(requested)  # type: ignore[assignment]
        self._issue(
            user=user,
            phase=CabinetPhase.WAIT_CHANNEL_ACK,
            changes={"channel_enable": tuple(values)},
            detail=f"CH{channel} {'enable' if enabled else 'disable'} request",
        )
        return True

    def request_reset_fault(self, *, user: str) -> bool:
        if (
            not self.client.connected
            or self.communication_state != "LIVE"
            or self.stale
        ):
            return self._reject(
                "ResetFaultPulse", user, "PLC communication is not live"
            )
        if self.status is None or not self.status.control_protocol_supported:
            return self._reject(
                "ResetFaultPulse", user,
                "旧协议仅可监控/无V1.2控制握手：故障复位需要配套PLC Host Protocol V1.2（0x0102）",
            )
        if self._pending is not None:
            return self._reject(
                "ResetFaultPulse",
                user,
                "another command is awaiting PLC acknowledgement",
            )
        if self.command.main_enable or any(self.command.channel_enable) or self.command.ps1_output_enable:
            return self._reject("ResetFaultPulse", user, "请先关闭主回路、通道和PS1输出请求，再复位PLC控制故障")
        old_phase = CabinetPhase.OFF if self.phase == CabinetPhase.FAULT else self.phase
        sequence = self.command.command_sequence + 1
        pulse = replace(
            self.command,
            reset_fault_pulse=True,
            command_sequence=sequence,
        )
        self._write_checked(pulse)
        # The retained image is FALSE immediately. The TRUE value is sent only
        # once and can therefore never be held by the heartbeat writer.
        self.command = replace(
            pulse,
            reset_fault_pulse=False,
        )
        log_index = self._append_log(
            user,
            "ResetFaultPulse",
            False,
            True,
            sequence,
            FeedbackState.PENDING,
            "复位脉冲已发送，等待PLC接收及复位结果；不复位PS1故障",
        )
        self.feedback["ResetFaultPulse"] = CommandFeedback(
            FeedbackState.PENDING,
            sequence,
            "复位脉冲已发送，等待PLC接收及复位结果；不复位PS1故障",
        )
        self._pending = _PendingCommand(
            sequence=sequence,
            deadline=self.clock() + self.timings.command_timeout_ms / 1000.0,
            next_phase=old_phase,
            controls=("ResetFaultPulse",),
            log_indexes=(log_index,),
        )
        self.phase = CabinetPhase.WAIT_RESET_RESULT
        return True

    def request_setpoints(
        self,
        voltage: float,
        current: float,
        *,
        user: str,
    ) -> bool:
        control = "Setpoints"
        if not self.setpoints.enabled:
            return self._reject(control, user, "PS1 setpoint control is disabled")
        if (
            not self.client.connected
            or self.communication_state != "LIVE"
            or self.stale
        ):
            return self._reject(control, user, "PLC communication is not live")
        if self._pending is not None:
            return self._reject(
                control, user, "another command is awaiting PLC acknowledgement"
            )
        if self.phase != CabinetPhase.OFF:
            return self._reject(
                control,
                user,
                "PS1 setpoints may only be changed while the cabinet is OFF",
            )
        if self.status is not None and (not self.status.control_protocol_supported or self.status.has_fault or self.status.ps1_manual_restart_locked):
            return self._reject(control, user, "设定需V1.2、无故障及无人工锁")
        if self.status is None or self.status.ps1_actual_output:
            return self._reject(
                control,
                user,
                "PS1 actual output must be OFF before changing setpoints",
            )
        if not self.status.ps1_comm_ok:
            return self._reject(
                control,
                user,
                "PS1 communication must be ready before changing setpoints",
            )
        try:
            voltage_value, current_value = self._validated_setpoints(voltage, current)
        except ValueError as exc:
            return self._reject(control, user, str(exc))
        return self._issue_setpoints(
            voltage_value,
            current_value,
            user=user,
            next_phase=CabinetPhase.OFF,
        )

    def _validated_setpoints(
        self,
        voltage: float,
        current: float,
    ) -> tuple[float, float]:
        if not self.setpoints.enabled:
            raise ValueError("PS1 setpoint control is disabled")
        voltage_value = float(voltage)
        current_value = float(current)
        if not math.isfinite(voltage_value) or not (
            self.setpoints.voltage_min_v
            <= voltage_value
            <= self.setpoints.voltage_max_v
        ):
            raise ValueError(
                "voltage must be within "
                f"[{self.setpoints.voltage_min_v:g}, "
                f"{self.setpoints.voltage_max_v:g}] V"
            )
        if not math.isfinite(current_value) or not (
            self.setpoints.current_min_a
            <= current_value
            <= self.setpoints.current_max_a
        ):
            raise ValueError(
                "current must be within "
                f"[{self.setpoints.current_min_a:g}, "
                f"{self.setpoints.current_max_a:g}] A"
            )
        return voltage_value, current_value

    def _issue_setpoints(
        self,
        voltage: float,
        current: float,
        *,
        user: str,
        next_phase: CabinetPhase,
    ) -> bool:
        assert self.status is not None
        old_value = {
            "voltage_v": self.status.ps1_set_voltage,
            "current_a": self.status.ps1_set_current,
        }
        new_value = {"voltage_v": voltage, "current_a": current}
        sequence = self.command.command_sequence + 1
        pulse = replace(
            self.command,
            set_voltage=voltage,
            set_current=current,
            apply_setpoints=True,
            command_sequence=sequence,
        )
        if not self._write_checked(pulse):
            return False
        self.command = replace(pulse, apply_setpoints=False)
        detail = "waiting for PLC setpoint application confirmation"
        log_index = self._append_log(
            user,
            "Setpoints",
            old_value,
            new_value,
            sequence,
            FeedbackState.PENDING,
            detail,
        )
        self.feedback["Setpoints"] = CommandFeedback(
            FeedbackState.PENDING,
            sequence,
            detail,
        )
        self.phase = next_phase
        self._pending = _PendingCommand(
            sequence=sequence,
            deadline=self.clock() + self.timings.command_timeout_ms / 1000.0,
            next_phase=next_phase,
            controls=("Setpoints",),
            log_indexes=(log_index,),
            setpoint_rejection_preexisting=self.status.setpoint_rejected,
        )
        return True

    def request_all_stop(self, *, user: str, detail: str = "operator all stop") -> bool:
        if (
            not self.client.connected
            or self.communication_state != "LIVE"
            or self.stale
        ):
            return self._reject("AllStop", user, "PLC communication is not live")
        if self._pending is not None:
            self._finish_pending(
                FeedbackState.REJECTED,
                "superseded by AllStop",
            )
        self._cancel_sequence_feedback("superseded by AllStop")
        self._start_requested = False
        self._start_setpoints = None
        self._requested_channels = (False, False, False, False)
        self._issue(
            user=user,
            phase=CabinetPhase.FAULT,
            changes={
                "channel_enable": (False, False, False, False),
                "ps1_output_enable": False,
                "main_enable": False,
                "all_stop": True,
            },
            detail=detail,
            force=True,
        )
        return True

    def heartbeat(self) -> None:
        if (
            not self.client.connected
            or self.communication_state != "LIVE"
            or self.stale
        ):
            return
        self.command = replace(
            self.command,
            heartbeat_counter=(self.command.heartbeat_counter + 1) & 0xFFFFFFFF,
            reset_fault_pulse=False,
        )
        self._write_checked(self.command)

    @property
    def startup_budget_ms(self) -> int:
        # Sum of the existing bounded acknowledgement/feedback stages plus
        # PLC V1.5's fixed MainReady delay. No retry can reset this deadline.
        return (5000 + self.timings.km0_timeout_ms + 3 * self.timings.ps1_timeout_ms
                + 9 * self.timings.command_timeout_ms + 2 * self.timings.poll_ms)

    def _output_admission_reason(self, *, channels: bool = False) -> str:
        status = self.status
        if status is None or self.communication_state != "LIVE" or self.stale:
            return "PLC TCP状态未确认或已过期"
        if not status.control_protocol_supported:
            return "控制需配套PLC Host Protocol V1.2（0x0102）"
        if status.has_fault:
            return "；".join(cabinet_fault_messages(status.to_dict())) or "PLC/PS1故障"
        if status.ps1_feedback_flags & 0xFF00 or status.ps1_manual_restart_locked:
            return "PLC人工重启锁/恢复耗尽或反馈标志非法"
        if not status.plc_ready or not status.host_connected or not status.main_ready:
            return "PLC/主回路尚未就绪"
        required = (Ps1FeedbackFlags.POWER_DELAY_DONE | Ps1FeedbackFlags.PORT_LOADED
                    | Ps1FeedbackFlags.STOP_OFF_CONFIRMED | Ps1FeedbackFlags.MEASUREMENT_VALID
                    | Ps1FeedbackFlags.SETPOINT_VALID)
        if (not self._ps1_feedback.fresh(status, self.clock())
                or status.ps1_feedback_flags & required != required):
            return "PS1本周期状态、测量、设定或STOP/OFF确认未齐备或已过期"
        if not status.ps1_comm_ok or status.analog_valid_mask != 0x0F:
            return "PS1设备未就绪或模拟量无效"
        if status.setpoint_rejected or not status.setpoint_applied:
            return "PS1设定未应用或被拒绝（仅撤运行许可）"
        if channels and not status.ps1_actual_output:
            return "PS1真实输出尚未确认ON"
        return ""

    def _write_checked(self, command: PlcCommand) -> bool:
        """Single final entry for every retained ON payload, including pulses/HB."""
        if not command.all_stop and (command.main_enable or command.ps1_output_enable or any(command.channel_enable)):
            status = self.status
            reason = ""
            if status is None or not status.control_protocol_supported or self.stale or self.communication_state != "LIVE":
                reason = "控制需V1.2及新鲜PLC TCP状态"
            elif status.has_fault:
                reason = "；".join(cabinet_fault_messages(status.to_dict()))
            elif status.ps1_manual_restart_locked or status.ps1_feedback_flags & 0xFF00:
                reason = "PLC人工重启锁/恢复耗尽或反馈标志非法"
            elif not status.plc_ready or not status.host_connected:
                reason = "PLC未就绪/主机连接未确认"
            elif command.ps1_output_enable or any(command.channel_enable):
                reason = self._output_admission_reason(channels=any(command.channel_enable))
                if not reason and any(enabled and physical is False for enabled, physical in zip(command.channel_enable, status.physical_permit, strict=True)):
                    reason = "通道物理许可缺失，禁止ON请求"
            if reason:
                # Fail closed before calling the transport, including same-frame
                # SetpointApplied + Fault. This path never sends the candidate ON.
                if (command.ps1_output_enable or any(command.channel_enable)) and self._only_setpoint_inhibited(self.clock()):
                    self._inhibit_outputs(reason)
                else:
                    self._enter_fault(reason, "system")
                return False
        self.client.write_command(command)
        return True

    def tick(self, now: float | None = None) -> None:
        current = self.clock() if now is None else float(now)
        if not self.client.connected:
            self._communication_lost("PLC transport disconnected")
            return
        try:
            self._read_and_resolve(current)
        except PlcCommunicationError as exc:
            self._communication_lost(str(exc))
            try:
                self.client.disconnect()
            except Exception:
                pass
            return
        assert self.status is not None
        if (
            current - self.status.received_monotonic
            > self.timings.stale_timeout_ms / 1000.0
        ):
            if self._pending is not None:
                self._finish_pending(FeedbackState.TIMEOUT, "PLC status exceeded stale timeout")
            self._cancel_sequence_feedback("PLC status exceeded stale timeout")
            self.communication_state = "STALE"
            self.phase = CabinetPhase.STALE
            self.last_error = "PLC status exceeded stale timeout"
            self._start_requested = False
            self._requested_channels = (False, False, False, False)
            return

        if self._start_requested and current >= self._startup_deadline:
            self._enter_fault("冷启动总时间预算耗尽；需人工重新检查并启动", "system")
            return

        if self.communication_state != "LIVE":
            # Transport recovered. Force every request OFF and require an
            # explicit new operator start.
            self.communication_state = "LIVE"
            self.last_error = ""
            self._start_requested = False
            self._requested_channels = (False, False, False, False)
            self.phase = CabinetPhase.OFF
            self._issue(
                user="system",
                phase=CabinetPhase.OFF,
                changes={
                    "main_enable": False,
                    "ps1_output_enable": False,
                    "channel_enable": (False, False, False, False),
                    "all_stop": False,
                },
                force=True,
                detail="communication recovered; outputs forced OFF",
            )
            return

        if self._pending is not None and self._pending.controls == ("ResetFaultPulse",):
            # The fault present before reset is expected during PLC judgement.
            # AcceptedCommandSeq is receipt only, never reset completion.
            if current >= self._pending.deadline:
                detail = "等待PLC复位结果超时；PLC控制故障是否清除未确认"
                self._finish_pending(FeedbackState.TIMEOUT, detail)
                self._enter_fault(detail, "system")
            return

        if self.status.has_fault:
            if self.phase != CabinetPhase.FAULT:
                self._enter_fault("；".join(cabinet_fault_messages(self.status.to_dict())), "system")
            return

        if self._pending is not None:
            if current >= self._pending.deadline:
                controls = self._pending.controls
                if controls == ("Setpoints",):
                    if self.status.setpoint_rejected:
                        self._finish_pending(
                            FeedbackState.REJECTED,
                            "PLC rejected the PS1 setpoints",
                        )
                        self.last_error = "PLC rejected the PS1 setpoints"
                        if self._start_requested:
                            self._enter_fault(
                                "PLC rejected the PS1 setpoints",
                                "system",
                            )
                        return
                    self._finish_pending(
                        FeedbackState.TIMEOUT,
                        "PS1 setpoint confirmation timeout",
                    )
                    self.last_error = "PS1 setpoint confirmation timeout"
                    if self._start_requested:
                        self._enter_fault(
                            "PS1 setpoint confirmation timeout",
                            "system",
                        )
                    return
                self._finish_pending(
                    FeedbackState.TIMEOUT,
                    "AcknowledgedSequence timeout",
                )
                self._enter_fault(
                    f"command acknowledgement timeout: {', '.join(controls)}",
                    "system",
                )
            return

        if self.phase == CabinetPhase.START_CLEAR_STOP:
            self._issue(
                user="operator",
                phase=CabinetPhase.START_CLEAR_ACK,
                changes={
                    "main_enable": False,
                    "ps1_output_enable": False,
                    "channel_enable": (False, False, False, False),
                    "all_stop": False,
                },
                force=True,
                detail="startup safety step 2/2: clear AllStop with outputs OFF",
            )
        elif self.phase == CabinetPhase.WAIT_PLC_READY:
            if self.status.plc_ready:
                self._issue(
                    user="operator",
                    phase=CabinetPhase.WAIT_MAIN_ACK,
                    changes={"main_enable": True},
                    detail="PLCReady confirmed",
                )
            elif current >= self._phase_deadline:
                self.feedback["Start"] = CommandFeedback(
                    FeedbackState.TIMEOUT, detail="PLCReady timeout"
                )
                self._enter_fault("PLCReady timeout", "system")
        elif self.phase == CabinetPhase.WAIT_MAIN_READY:
            if self.status.main_ready and self.status.main_contactor_fb is not False:
                self.phase = CabinetPhase.WAIT_PS1_COMM
                # HR28 bit0 is only the fixed 5s MainReady timer, not self-test.
                self._phase_deadline = current + (5000 + self.timings.ps1_timeout_ms + 2 * self.timings.poll_ms) / 1000.0
            elif current >= self._phase_deadline:
                self.feedback["MainEnable"] = CommandFeedback(
                    FeedbackState.TIMEOUT,
                    self.command.command_sequence,
                    "KM0 feedback/MainReady timeout",
                )
                self._enter_fault("KM0 feedback/MainReady timeout", "system")
                self.feedback["MainEnable"] = CommandFeedback(
                    FeedbackState.TIMEOUT,
                    self.command.command_sequence,
                    "KM0 feedback/MainReady timeout",
                )
        elif self.phase == CabinetPhase.WAIT_PS1_COMM:
            if not self.status.main_ready:
                self._enter_fault("MainReady was lost before PS1 startup", "system")
            elif self.status.ps1_comm_ok:
                self._issue(
                    user="operator",
                    phase=CabinetPhase.WAIT_PS1_STOP_ACK,
                    changes={"ps1_output_enable": False},
                    force=True,
                    detail=(
                        "PS1 communication ready; explicitly request output OFF "
                        "before setpoint application"
                    ),
                )
            elif current >= self._phase_deadline:
                self.feedback["PS1OutputEnable"] = CommandFeedback(
                    FeedbackState.TIMEOUT,
                    self.command.command_sequence,
                    "PS1 communication timeout",
                )
                self._enter_fault("PS1 communication timeout", "system")
        elif self.phase == CabinetPhase.WAIT_PS1_STOP_READY:
            if not self.status.main_ready:
                self._enter_fault("MainReady was lost before PS1 startup", "system")
            elif (
                self.status.ps1_comm_ok
                and not self.status.ps1_actual_output
                and not self.status.ps1_comm_busy
                and self._ps1_feedback.fresh(self.status, current)
                and self.status.ps1_feedback_flags & Ps1FeedbackFlags.STOP_OFF_CONFIRMED
                and self.status.ps1_feedback_flags & Ps1FeedbackFlags.MEASUREMENT_VALID
                and self.status.ps1_feedback_flags & Ps1FeedbackFlags.SETPOINT_VALID
            ):
                self._continue_start_after_ps1_stop()
            elif current >= self._phase_deadline:
                self.feedback["PS1OutputEnable"] = CommandFeedback(
                    FeedbackState.TIMEOUT,
                    self.command.command_sequence,
                    "PS1 STOP confirmation timeout",
                )
                self._enter_fault("PS1 STOP confirmation timeout", "system")
        elif self.phase == CabinetPhase.WAIT_PS1_OUTPUT:
            if self.status.ps1_comm_ok and self.status.ps1_actual_output:
                self._enable_permitted_channels()
            elif current >= self._phase_deadline:
                detail = (
                    "PS1 communication timeout"
                    if not self.status.ps1_comm_ok
                    else "PS1 actual output timeout"
                )
                self.feedback["PS1OutputEnable"] = CommandFeedback(
                    FeedbackState.TIMEOUT,
                    self.command.command_sequence,
                    detail,
                )
                self._enter_fault(detail, "system")
                self.feedback["PS1OutputEnable"] = CommandFeedback(
                    FeedbackState.TIMEOUT,
                    self.command.command_sequence,
                    detail,
                )
        elif self.phase == CabinetPhase.WAIT_CHANNEL_PERMIT:
            expected = self.command.channel_enable
            if all(
                (not wanted) or permitted
                for wanted, permitted in zip(
                    expected, self.status.channel_permit, strict=True
                )
            ):
                self._finish_startup()
                self.feedback["Start"] = CommandFeedback(
                    FeedbackState.CONFIRMED,
                    self.command.command_sequence,
                    "power sequence complete",
                )
            elif current >= self._phase_deadline:
                missing = tuple(
                    wanted and not permitted
                    for wanted, permitted in zip(
                        expected, self.status.channel_permit, strict=True
                    )
                )
                retained = tuple(
                    wanted and not absent
                    for wanted, absent in zip(expected, missing, strict=True)
                )
                for index, absent in enumerate(missing, start=1):
                    if absent:
                        self._reject(
                            f"ChannelEnable{index}",
                            "system",
                            "PLC final ChannelFlags did not confirm the channel; "
                            "physical permit may be absent",
                        )
                if any(retained):
                    self._requested_channels = retained
                    self._issue(
                        user="system",
                        phase=CabinetPhase.WAIT_CHANNEL_ACK,
                        changes={"channel_enable": retained},
                        detail="unconfirmed channels disabled after permit timeout",
                    )
                else:
                    self.feedback["Start"] = CommandFeedback(
                        FeedbackState.REJECTED,
                        self.command.command_sequence,
                        "no selected channel received PLC final permit",
                    )
                    self._start_requested = False
                    self._requested_channels = (False, False, False, False)
                    self._begin_stop("system")
        elif self.phase == CabinetPhase.RUNNING:
            if not (
                self.status.main_ready
                and self.status.main_contactor_fb is not False
                and self.status.ps1_comm_ok
                and self.status.ps1_actual_output
            ):
                self._enter_fault("running power feedback was lost", "system")
                return
            lost = tuple(
                enabled and physical is False
                for enabled, physical in zip(
                    self.command.channel_enable,
                    self.status.physical_permit,
                    strict=True,
                )
            )
            if any(lost):
                values = tuple(
                    enabled and not missing
                    for enabled, missing in zip(
                        self.command.channel_enable, lost, strict=True
                    )
                )
                self._issue(
                    user="system",
                    phase=CabinetPhase.WAIT_CHANNEL_ACK,
                    changes={"channel_enable": values},
                    detail="physical permit lost; affected channel disabled",
                )
        elif self.phase == CabinetPhase.WAIT_CHANNELS_OFF:
            if not any(self.status.channel_permit):
                self._stop_ps1("operator")
            elif current >= self._phase_deadline:
                self._enter_fault("channel shutdown feedback timeout", "system")
        elif self.phase == CabinetPhase.WAIT_PS1_OFF:
            if self._ps1_feedback.fresh(self.status, current) and not self.status.ps1_actual_output:
                self._stop_main("operator")
            elif current >= self._phase_deadline:
                self._enter_fault("PS1 shutdown feedback timeout", "system")
        elif self.phase == CabinetPhase.WAIT_MAIN_OFF:
            if self.status.main_contactor_fb is not True and not self.status.main_ready:
                self.phase = CabinetPhase.OFF
                self.feedback["Stop"] = CommandFeedback(
                    FeedbackState.CONFIRMED,
                    self.command.command_sequence,
                    "channels, PS1 and main contactor are OFF",
                )
            elif current >= self._phase_deadline:
                self._enter_fault("KM0 release feedback timeout", "system")

    def _read_and_resolve(self, now: float) -> None:
        previous = self.status
        self.status = self.client.read_status()
        if now - self.status.received_monotonic > self.timings.stale_timeout_ms / 1000.0:
            return  # No acknowledgement or ON transition from a stale TCP frame.
        boundary = self._ps1_feedback.observe(self.status, now)
        reset_pending = self._pending is not None and self._pending.controls == ("ResetFaultPulse",)
        if not reset_pending:
            if self.status.has_fault:
                if self.phase != CabinetPhase.FAULT:
                    self._enter_fault("；".join(cabinet_fault_messages(self.status.to_dict())), "system")
                    return
            active = self.command.main_enable or self.command.ps1_output_enable or any(self.command.channel_enable)
            expected_power_on = (previous is not None and not previous.main_ready and self.status.main_ready
                                 and self.phase in {CabinetPhase.WAIT_MAIN_ACK, CabinetPhase.WAIT_MAIN_READY})
            if boundary and active and not expected_power_on:
                self._enter_fault("PS1上电周期/CPU状态发生变化；撤销本地确认并要求人工重新启动", "system")
                return
            if active and (not self.status.control_protocol_supported or self.status.ps1_manual_restart_locked or self.status.ps1_feedback_flags & 0xFF00 or not self.status.host_connected or not self.status.plc_ready):
                if self.phase != CabinetPhase.FAULT:
                    self._enter_fault("PLC协议/人工锁/恢复耗尽/反馈标志不允许控制", "system")
                    return
            if self.command.ps1_output_enable or any(self.command.channel_enable):
                reason = self._output_admission_reason()
                if reason:
                    if self._only_setpoint_inhibited(now):
                        self._inhibit_outputs(reason)
                    else:
                        self._enter_fault("PS1反馈周期变化/已过期" if boundary else reason, "system")
                    return
        if (
            self._pending is not None
            and sequence_reached(self.status.acknowledged_sequence, self._pending.sequence)
        ):
            if self._pending.controls == ("ResetFaultPulse",):
                self._resolve_reset_result()
                return
            if self._pending.controls == ("Setpoints",):
                next_phase = self._pending.next_phase
                voltage_matches = self.command.set_voltage is not None and abs(
                    self.status.ps1_set_voltage - self.command.set_voltage
                ) <= max(self.setpoints.step / 2.0, 1e-9)
                current_matches = self.command.set_current is not None and abs(
                    self.status.ps1_set_current - self.command.set_current
                ) <= max(self.setpoints.step / 2.0, 1e-9)
                if (self.status.setpoint_applied and not self.status.setpoint_rejected
                        and self._ps1_feedback.fresh(self.status, now)
                        and self.status.ps1_feedback_flags & Ps1FeedbackFlags.SETPOINT_VALID
                        and voltage_matches and current_matches):
                    self._finish_pending(
                        FeedbackState.CONFIRMED,
                        "PLC applied and echoed the PS1 setpoints",
                    )
                    self.last_error = ""
                    self._start_setpoints = None
                    if (
                        next_phase == CabinetPhase.WAIT_PS1_ACK
                        and self._start_requested
                    ):
                        self._start_ps1_output("operator")
                    else:
                        self.phase = next_phase
                    return
                if not self.status.setpoint_rejected:
                    self._pending.setpoint_rejection_cleared = True
                elif (
                    not self._pending.setpoint_rejection_preexisting
                    or self._pending.setpoint_rejection_cleared
                ):
                    self._finish_pending(
                        FeedbackState.REJECTED,
                        "PLC rejected the PS1 setpoints",
                    )
                    self.last_error = "PLC rejected the PS1 setpoints"
                    if self._start_requested:
                        self._enter_fault("PLC rejected the PS1 setpoints", "system")
                    return
                # A rejection bit from the previous transaction can remain set
                # while the PLC acknowledges and starts processing this one.
                # Do not misclassify that stale flag; wait for it to clear,
                # become a fresh rejection, or reach the command deadline.
                return
            next_phase = self._pending.next_phase
            self._finish_pending(FeedbackState.CONFIRMED, "PLC sequence acknowledged")
            self.phase = next_phase
            if self.command.all_stop:
                self.command = replace(self.command, all_stop=False)
            if next_phase == CabinetPhase.WAIT_MAIN_READY:
                self._phase_deadline = now + self.timings.km0_timeout_ms / 1000.0
            elif next_phase == CabinetPhase.WAIT_PLC_READY:
                self._phase_deadline = now + self.timings.command_timeout_ms / 1000.0
            elif next_phase in {
                CabinetPhase.WAIT_PS1_STOP_READY,
                CabinetPhase.WAIT_PS1_OUTPUT,
                CabinetPhase.WAIT_PS1_OFF,
            }:
                self._phase_deadline = now + self.timings.ps1_timeout_ms / 1000.0
            elif next_phase in {
                CabinetPhase.WAIT_CHANNEL_PERMIT,
                CabinetPhase.WAIT_CHANNELS_OFF,
                CabinetPhase.WAIT_MAIN_OFF,
            }:
                self._phase_deadline = now + self.timings.command_timeout_ms / 1000.0

    def _resolve_reset_result(self) -> None:
        assert self.status is not None and self._pending is not None
        if self.status.reset_ack_sequence != (self._pending.sequence & 0xFFFF):
            return
        result = self.status.reset_result
        if result in {ResetResult.IDLE, ResetResult.PENDING}:
            return
        if result == ResetResult.SUCCEEDED:
            self._finish_pending(FeedbackState.CONFIRMED, "PLC控制故障复位成功（不代表PS1通信或设备故障已清除）")
            self.phase = CabinetPhase.FAULT if self.status.has_fault else CabinetPhase.OFF
            self.last_error = "；".join(cabinet_fault_messages(self.status.to_dict()))
        elif result == ResetResult.REJECTED_NOT_SAFE:
            detail = "PLC拒绝复位：主回路/通道请求未全关闭，或KM0反馈仍在"
            self._finish_pending(FeedbackState.REJECTED, detail)
            self.phase = CabinetPhase.FAULT
            self.last_error = detail
        elif result == ResetResult.FAILED_STILL_LATCHED:
            detail = "PLC复位失败：等待超时后PLC控制故障仍锁存"
            self._finish_pending(FeedbackState.FAILED, detail)
            self.phase = CabinetPhase.FAULT
            self.last_error = detail
        else:
            detail = f"PLC复位结果无效（ResetResult={result}），执行结果未确认"
            self._finish_pending(FeedbackState.FAILED, detail)
            self.phase = CabinetPhase.FAULT
            self.last_error = detail

    def _issue(
        self,
        *,
        user: str,
        phase: CabinetPhase,
        changes: dict[str, Any],
        detail: str,
        force: bool = False,
    ) -> None:
        if self._pending is not None:
            raise RuntimeError("a PLC command is already pending acknowledgement")
        old = self.command
        changed = [
            (name, getattr(old, name), value)
            for name, value in changes.items()
            if force or getattr(old, name) != value
        ]
        sequence = old.command_sequence + 1
        candidate = replace(old, **changes, command_sequence=sequence)
        if not self._write_checked(candidate):
            return
        self.command = candidate
        controls: list[str] = []
        indexes: list[int] = []
        for field, old_value, new_value in changed:
            control = self._control_name(field, old_value, new_value)
            controls.append(control)
            self.feedback[control] = CommandFeedback(
                FeedbackState.PENDING,
                sequence,
                detail,
            )
            if field == "channel_enable":
                for index, (before, after) in enumerate(zip(old_value, new_value), 1):
                    if force or before != after:
                        name = f"ChannelEnable{index}"
                        controls.append(name)
                        self.feedback[name] = CommandFeedback(
                            FeedbackState.PENDING, sequence, detail,
                        )
            indexes.append(
                self._append_log(
                    user,
                    control,
                    old_value,
                    new_value,
                    sequence,
                    FeedbackState.PENDING,
                    detail,
                )
            )
        if not controls:
            controls = ["SafeInitialize"]
            indexes.append(
                self._append_log(
                    user,
                    "SafeInitialize",
                    "unknown",
                    "all requests OFF",
                    sequence,
                    FeedbackState.PENDING,
                    detail,
                )
            )
        self.phase = phase
        self._pending = _PendingCommand(
            sequence=sequence,
            deadline=self.clock() + self.timings.command_timeout_ms / 1000.0,
            next_phase=self._phase_after_ack(phase),
            controls=tuple(controls),
            log_indexes=tuple(indexes),
        )

    @staticmethod
    def _phase_after_ack(phase: CabinetPhase) -> CabinetPhase:
        return {
            CabinetPhase.START_STOP_ACK: CabinetPhase.START_CLEAR_STOP,
            CabinetPhase.START_CLEAR_ACK: CabinetPhase.WAIT_PLC_READY,
            CabinetPhase.WAIT_MAIN_ACK: CabinetPhase.WAIT_MAIN_READY,
            CabinetPhase.WAIT_PS1_STOP_ACK: CabinetPhase.WAIT_PS1_STOP_READY,
            CabinetPhase.WAIT_PS1_ACK: CabinetPhase.WAIT_PS1_OUTPUT,
            CabinetPhase.WAIT_CHANNEL_ACK: CabinetPhase.WAIT_CHANNEL_PERMIT,
            CabinetPhase.STOP_CHANNEL_ACK: CabinetPhase.WAIT_CHANNELS_OFF,
            CabinetPhase.STOP_PS1_ACK: CabinetPhase.WAIT_PS1_OFF,
            CabinetPhase.STOP_MAIN_ACK: CabinetPhase.WAIT_MAIN_OFF,
        }.get(phase, phase)

    @staticmethod
    def _control_name(field: str, old_value: Any, new_value: Any) -> str:
        del old_value
        if field == "channel_enable":
            # A group transaction still logs individual channel values below
            # in the serialized old/new tuples.
            return "ChannelEnable"
        return {
            "main_enable": "MainEnable",
            "ps1_output_enable": "PS1OutputEnable",
            "all_stop": "AllStop",
        }.get(field, field)

    def _start_ps1_output(self, user: str) -> None:
        self._issue(
            user=user,
            phase=CabinetPhase.WAIT_PS1_ACK,
            changes={"ps1_output_enable": True},
            detail="MainReady, PS1 communication and setpoints confirmed",
        )

    def _continue_start_after_ps1_stop(self) -> None:
        if self._start_setpoints is None:
            self._start_ps1_output("operator")
            return
        voltage, current_limit = self._start_setpoints
        if self._setpoints_match_status(voltage, current_limit):
            self._confirm_matching_setpoints(voltage, current_limit)
            self._start_ps1_output("operator")
            return
        self._issue_setpoints(
            voltage,
            current_limit,
            user="operator",
            next_phase=CabinetPhase.WAIT_PS1_ACK,
        )

    def _setpoints_match_status(self, voltage: float, current: float) -> bool:
        assert self.status is not None
        tolerance = max(self.setpoints.step / 2.0, 1e-9)
        return (
            self._ps1_feedback.fresh(self.status, self.clock())
            and bool(self.status.ps1_feedback_flags & Ps1FeedbackFlags.SETPOINT_VALID)
            and self.status.setpoint_applied and not self.status.setpoint_rejected
            and abs(self.status.ps1_set_voltage - voltage) <= tolerance
            and abs(self.status.ps1_set_current - current) <= tolerance
        )

    def _confirm_matching_setpoints(self, voltage: float, current: float) -> None:
        assert self.status is not None
        value = {"voltage_v": voltage, "current_a": current}
        detail = "PLC setpoint readback already matches; redundant write skipped"
        sequence = self.status.acknowledged_sequence
        self.feedback["Setpoints"] = CommandFeedback(
            FeedbackState.CONFIRMED,
            sequence,
            detail,
        )
        self._append_log(
            "operator",
            "Setpoints",
            value,
            value,
            sequence,
            FeedbackState.CONFIRMED,
            detail,
        )
        self._start_setpoints = None

    def _finish_startup(self) -> None:
        """End the startup budget; RUNNING retains all runtime health guards."""
        self._start_requested = False
        self._startup_deadline = 0.0
        self.phase = CabinetPhase.RUNNING

    def _enable_permitted_channels(self) -> None:
        assert self.status is not None
        if not any(self._requested_channels):
            self._finish_startup()
            self.feedback["Start"] = CommandFeedback(
                FeedbackState.CONFIRMED,
                self.command.command_sequence,
                "main circuit and PS1 are ready; no channels selected",
            )
            return
        enabled: list[bool] = []
        for index, (requested, permitted) in enumerate(
            zip(self._requested_channels, self.status.physical_permit, strict=True),
            start=1,
        ):
            # ``None`` is deliberately distinct from False: Modbus V1.0 does
            # not expose raw S1-S4 inputs.  In that case the request is sent and
            # the PLC's final ChannelFlags remains the authority.
            accepted = bool(requested and permitted is not False)
            enabled.append(accepted)
            if requested and permitted is False:
                self._reject(
                    f"ChannelEnable{index}",
                    "operator",
                    "physical permit is absent",
                )
        if not any(enabled):
            self._finish_startup()
            self.feedback["Start"] = CommandFeedback(
                FeedbackState.REJECTED,
                detail="no selected channel has physical permit",
            )
            return
        self._issue(
            user="operator",
            phase=CabinetPhase.WAIT_CHANNEL_ACK,
            changes={"channel_enable": tuple(enabled)},
            detail="PS1 actual output and physical permit confirmed",
        )

    def _begin_stop(self, user: str) -> None:
        assert self.status is not None
        if any(self.command.channel_enable) or any(self.status.channel_permit):
            self._issue(
                user=user,
                phase=CabinetPhase.STOP_CHANNEL_ACK,
                changes={"channel_enable": (False, False, False, False)},
                detail="normal stop step 1/3: channels OFF",
                force=True,
            )
        else:
            self._stop_ps1(user)

    def _stop_ps1(self, user: str) -> None:
        assert self.status is not None
        if self.command.ps1_output_enable or self.status.ps1_actual_output or not self._ps1_feedback.fresh(self.status, self.clock()):
            self._issue(
                user=user,
                phase=CabinetPhase.STOP_PS1_ACK,
                changes={"ps1_output_enable": False},
                detail="normal stop step 2/3: PS1 output OFF",
                force=True,
            )
        else:
            self._stop_main(user)

    def _stop_main(self, user: str) -> None:
        assert self.status is not None
        if (
            self.command.main_enable
            or self.status.main_contactor_fb is True
            or self.status.main_ready
        ):
            self._issue(
                user=user,
                phase=CabinetPhase.STOP_MAIN_ACK,
                changes={"main_enable": False},
                detail="normal stop step 3/3: main contactor OFF",
                force=True,
            )
        else:
            self.phase = CabinetPhase.OFF
            self.feedback["Stop"] = CommandFeedback(
                FeedbackState.CONFIRMED,
                self.command.command_sequence,
                "already OFF",
            )

    def _finish_pending(self, state: FeedbackState, detail: str) -> None:
        pending = self._pending
        if pending is None:
            return
        for control in pending.controls:
            self.feedback[control] = CommandFeedback(
                state,
                pending.sequence,
                detail,
            )
        for index in pending.log_indexes:
            entry = self.operation_log[index]
            entry.confirmation = state
            entry.detail = detail
        self._pending = None

    def _inhibit_outputs(self, detail: str) -> None:
        """F003/setpoint mismatch withdraws run permission, retaining healthy KM0."""
        if self._pending is not None:
            self._finish_pending(FeedbackState.REJECTED, detail)
        self._start_requested = False
        self._start_setpoints = None
        self._requested_channels = (False,) * 4
        self.last_error = detail
        self.feedback["Start"] = CommandFeedback(FeedbackState.FAILED, detail=detail)
        self._issue(user="system", phase=CabinetPhase.FAULT,
                    changes={"channel_enable": (False,) * 4, "ps1_output_enable": False},
                    detail=detail, force=True)

    def _only_setpoint_inhibited(self, now: float) -> bool:
        status = self.status
        required = int(Ps1FeedbackFlags.POWER_DELAY_DONE | Ps1FeedbackFlags.PORT_LOADED |
                       Ps1FeedbackFlags.STATUS_VALID | Ps1FeedbackFlags.STOP_OFF_CONFIRMED |
                       Ps1FeedbackFlags.MEASUREMENT_VALID)
        return bool(status and not status.has_fault and not status.ps1_manual_restart_locked
                    and not status.ps1_feedback_flags & 0xFF00 and status.ps1_comm_ok
                    and status.main_ready and status.analog_valid_mask == 0x0F
                    and status.ps1_feedback_flags & required == required
                    and self._ps1_feedback.fresh(status, now)
                    and (status.setpoint_rejected or status.ps1_mb_status == 0xF003))

    def _reject(self, control: str, user: str, detail: str) -> bool:
        self.feedback[control] = CommandFeedback(FeedbackState.REJECTED, detail=detail)
        self._append_log(
            user,
            control,
            "unchanged",
            "unchanged",
            None,
            FeedbackState.REJECTED,
            detail,
        )
        return False

    def _append_log(
        self,
        user: str,
        field: str,
        old_value: Any,
        new_value: Any,
        sequence: int | None,
        confirmation: FeedbackState,
        detail: str,
    ) -> int:
        self.operation_log.append(
            OperationLogEntry(
                timestamp=self._timestamp(),
                user=user or "unknown",
                field=field,
                old_value=old_value,
                new_value=new_value,
                command_sequence=sequence,
                confirmation=confirmation,
                detail=detail,
            )
        )
        return len(self.operation_log) - 1

    def _enter_fault(self, detail: str, user: str) -> None:
        start_in_progress = self._start_requested or self.phase in {
            CabinetPhase.WAIT_PLC_READY,
            CabinetPhase.START_STOP_ACK,
            CabinetPhase.START_CLEAR_STOP,
            CabinetPhase.START_CLEAR_ACK,
            CabinetPhase.WAIT_MAIN_ACK,
            CabinetPhase.WAIT_MAIN_READY,
            CabinetPhase.WAIT_PS1_COMM,
            CabinetPhase.WAIT_PS1_STOP_ACK,
            CabinetPhase.WAIT_PS1_STOP_READY,
            CabinetPhase.WAIT_PS1_ACK,
            CabinetPhase.WAIT_PS1_OUTPUT,
            CabinetPhase.WAIT_CHANNEL_ACK,
            CabinetPhase.WAIT_CHANNEL_PERMIT,
        }
        stop_in_progress = self.phase in {
            CabinetPhase.STOP_CHANNEL_ACK,
            CabinetPhase.WAIT_CHANNELS_OFF,
            CabinetPhase.STOP_PS1_ACK,
            CabinetPhase.WAIT_PS1_OFF,
            CabinetPhase.STOP_MAIN_ACK,
            CabinetPhase.WAIT_MAIN_OFF,
        }
        self.last_error = detail
        self._start_requested = False
        self._start_setpoints = None
        self._requested_channels = (False, False, False, False)
        if self._pending is not None:
            self._finish_pending(FeedbackState.TIMEOUT, detail)
        if (
            self.client.connected
            and self.communication_state == "LIVE"
            and not self.stale
        ):
            try:
                self._issue(
                    user=user,
                    phase=CabinetPhase.FAULT,
                    changes={
                        "channel_enable": (False, False, False, False),
                        "ps1_output_enable": False,
                        "main_enable": False,
                        "all_stop": True,
                    },
                    detail=detail,
                    force=True,
                )
                if start_in_progress:
                    self.feedback["Start"] = CommandFeedback(
                        FeedbackState.TIMEOUT,
                        self.command.command_sequence,
                        detail,
                    )
                if stop_in_progress:
                    self.feedback["Stop"] = CommandFeedback(
                        FeedbackState.TIMEOUT,
                        self.command.command_sequence,
                        detail,
                    )
                return
            except PlcCommunicationError:
                pass
        self.command = PlcCommand(command_sequence=self.command.command_sequence)
        self._ps1_feedback.reset()
        self.phase = CabinetPhase.FAULT
        if start_in_progress:
            self.feedback["Start"] = CommandFeedback(
                FeedbackState.TIMEOUT,
                self.command.command_sequence,
                detail,
            )
        if stop_in_progress:
            self.feedback["Stop"] = CommandFeedback(
                FeedbackState.TIMEOUT,
                self.command.command_sequence,
                detail,
            )

    def _communication_lost(self, detail: str) -> None:
        self._ps1_feedback.reset()
        self._cancel_sequence_feedback(detail)
        if self._pending is not None:
            self._finish_pending(FeedbackState.TIMEOUT, detail)
        self.communication_state = "COMM_LOST"
        self.phase = CabinetPhase.COMM_LOST
        self.last_error = detail
        self._start_requested = False
        self._start_setpoints = None
        self._requested_channels = (False, False, False, False)
        self.command = PlcCommand(command_sequence=self.command.command_sequence)

    def notify_cpu_restart(self) -> None:
        """Invalidate explicitly known restarts; 16-bit counters are not boot IDs."""
        self._ps1_feedback.reset()
        self._enter_fault("已知PLC CPU重启：本地反馈确认已撤销，需人工重新启动", "system")

    def snapshot(self) -> dict[str, Any]:
        stale = self.communication_state == "STALE" or self.stale
        status = self.status.to_dict(stale=stale, ps1_fresh=self._ps1_feedback.fresh(self.status, self.clock())) if self.status else None
        return {
            "phase": self.phase.value,
            "communication_state": self.communication_state,
            "stale": stale,
            "last_error": self.last_error,
            "output_admission": {"allowed": not bool(self._output_admission_reason()), "reason": self._output_admission_reason()},
            "ps1_progress_age_ms": self._ps1_feedback.age_ms(self.clock()),
            "startup_budget_ms": self.startup_budget_ms,
            "status": status,
            "command": self.command.to_dict(),
            "feedback": {key: value.to_dict() for key, value in self.feedback.items()},
            "operation_log": [entry.to_dict() for entry in self.operation_log[-200:]],
        }


__all__ = ["PlcCabinetController"]
