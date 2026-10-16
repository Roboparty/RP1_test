"""Protocol clients for PLC cabinet communication."""

from __future__ import annotations

import math
import threading
import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import replace
from typing import Any

from .models import FaultSource, PlcCommand, PlcStatus, PROTOCOL_V10, PROTOCOL_V11, PROTOCOL_V12, ResetResult


class PlcCommunicationError(RuntimeError):
    pass


class PlcConfigurationError(RuntimeError):
    pass


class PlcClient(ABC):
    """Only contract visible to the cabinet business state machine."""

    @property
    @abstractmethod
    def connected(self) -> bool: ...

    @abstractmethod
    def connect(self) -> None: ...

    @abstractmethod
    def disconnect(self) -> None: ...

    @abstractmethod
    def read_status(self) -> PlcStatus: ...

    @abstractmethod
    def write_command(self, command: PlcCommand) -> None: ...


class ReadOnlyPlcClient(PlcClient):
    """Enforce read-only access even if a controller accidentally attempts a write."""

    def __init__(self, client: PlcClient) -> None:
        self.client = client

    @property
    def connected(self) -> bool:
        return self.client.connected

    def connect(self) -> None:
        self.client.connect()

    def disconnect(self) -> None:
        self.client.disconnect()

    def read_status(self) -> PlcStatus:
        return self.client.read_status()

    def write_command(self, command: PlcCommand) -> None:
        raise PlcConfigurationError("PLC 处于实机只读监控模式，禁止写命令")


class MockPlcClient(PlcClient):
    """Deterministic in-memory PLC used by the UI and automated tests."""

    def __init__(
        self,
        *,
        auto_ack: bool = True,
        auto_main_ready: bool = True,
        auto_ps1_output: bool = True,
        physical_permit: tuple[bool | None, bool | None, bool | None, bool | None] = (
            True,
            True,
            True,
            True,
        ),
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.auto_ack = auto_ack
        self.auto_main_ready = auto_main_ready
        self.auto_ps1_output = auto_ps1_output
        self._clock = clock
        self.refresh_ps1_on_read = True
        self._connected = False
        self._lock = threading.RLock()
        self.last_command = PlcCommand()
        self.command_history: list[PlcCommand] = []
        self._status = PlcStatus(
            protocol_magic=0x4C46,
            protocol_version=PROTOCOL_V12,
            ps1_feedback_flags=0xCF,
            ps1_status_age_ms=0,
            setpoint_applied=True,
            plc_ready=True,
            host_connected=True,
            ps1_comm_ok=True,
            physical_permit=physical_permit,
            analog_valid_mask=0b1111,
        )
        self.refresh_on_read = True

    @property
    def connected(self) -> bool:
        with self._lock:
            return self._connected

    def connect(self) -> None:
        with self._lock:
            self._connected = True
            self._status = replace(
                self._status,
                received_monotonic=self._clock(),
                status_counter=self._status.status_counter + 1,
            )

    def disconnect(self) -> None:
        with self._lock:
            self._connected = False

    def read_status(self) -> PlcStatus:
        with self._lock:
            if not self._connected:
                raise PlcCommunicationError("mock PLC is disconnected")
            if self.refresh_on_read:
                self._status = replace(
                    self._status,
                    received_monotonic=self._clock(),
                    status_counter=self._status.status_counter + 1,
                )
                if self.refresh_ps1_on_read:
                    self._status = replace(self._status,
                        ps1_status_sequence=(self._status.ps1_status_sequence + 1) & 0xFFFF)
            return self._status

    def write_command(self, command: PlcCommand) -> None:
        with self._lock:
            if not self._connected:
                raise PlcCommunicationError("mock PLC is disconnected")
            self.last_command = command
            self.command_history.append(command)
            main = command.main_enable and not command.all_stop
            ps1_request = command.ps1_output_enable and main and not command.all_stop
            main_fb = main and self.auto_main_ready
            main_ready = main_fb and self._status.plc_ready
            cycle = self._status.ps1_power_cycle
            if main_ready and not self._status.main_ready:
                cycle = (cycle + 1) & 0xFFFF
            ps1_actual = (
                ps1_request
                and main_ready
                and self._status.ps1_comm_ok
                and not self._status.ps1_fault
                and self.auto_ps1_output
            )
            channel_permit = tuple(
                bool(
                    enabled
                    and ps1_actual
                    and physical is not False
                    and not self._status.fault_latched
                )
                for enabled, physical in zip(
                    command.channel_enable,
                    self._status.physical_permit,
                    strict=True,
                )
            )
            fault_latched = self._status.fault_latched
            fault_code = self._status.fault_code
            if command.reset_fault_pulse:
                safe = not command.main_enable and not any(command.channel_enable)
                safe = safe and self._status.main_contactor_fb is not True
                if safe:
                    fault_latched = False
                    fault_code = 0
                    reset_result = ResetResult.SUCCEEDED
                else:
                    reset_result = ResetResult.REJECTED_NOT_SAFE
                self._status = replace(
                    self._status,
                    reset_ack_sequence=command.command_sequence & 0xFFFF,
                    reset_result=reset_result,
                    fault_source_flags=(self._status.fault_source_flags & ~int(FaultSource.PLC_LATCHED)
                                        if safe else self._status.fault_source_flags),
                )
            set_voltage = self._status.ps1_set_voltage
            set_current = self._status.ps1_set_current
            setpoint_applied = self._status.setpoint_applied
            setpoint_rejected = self._status.setpoint_rejected
            if command.apply_setpoints:
                if command.set_voltage is None or command.set_current is None:
                    raise PlcConfigurationError(
                        "setpoint command requires voltage and current"
                    )
                set_voltage = float(command.set_voltage)
                set_current = float(command.set_current)
                setpoint_applied = True
                setpoint_rejected = False
            self._status = replace(
                self._status,
                host_connected=True,
                ps1_power_cycle=cycle,
                main_contactor_fb=main_fb,
                main_ready=main_ready,
                ps1_request=ps1_request,
                ps1_actual_output=ps1_actual,
                channel_permit=channel_permit,
                fault_latched=fault_latched,
                fault_code=fault_code,
                ps1_set_voltage=set_voltage,
                ps1_set_current=set_current,
                setpoint_applied=setpoint_applied,
                setpoint_rejected=setpoint_rejected,
                acknowledged_sequence=(
                    command.command_sequence
                    if self.auto_ack
                    else self._status.acknowledged_sequence
                ),
                heartbeat_echo=command.heartbeat_counter & 0xFFFF,
                status_counter=self._status.status_counter + 1,
                received_monotonic=self._clock(),
            )

    def set_status(self, **changes: Any) -> None:
        with self._lock:
            self._status = replace(self._status, **changes)


class _UnfrozenAddressClient(PlcClient):
    protocol_name = "PLC"

    def __init__(self, config: Mapping[str, Any]) -> None:
        self.config = dict(config)
        self._connected = False

    @property
    def connected(self) -> bool:
        return self._connected

    def _raise_unfrozen(self) -> None:
        raise PlcConfigurationError(
            f"{self.protocol_name} adapter is disabled: PLC address mapping is still TODO. "
            "Fill factory_hmi/config/plc_cabinet.yaml after the PLC project is frozen."
        )

    def connect(self) -> None:
        self._raise_unfrozen()

    def disconnect(self) -> None:
        self._connected = False

    def read_status(self) -> PlcStatus:
        self._raise_unfrozen()

    def write_command(self, command: PlcCommand) -> None:
        del command
        self._raise_unfrozen()


class S7PlcClient(_UnfrozenAddressClient):
    protocol_name = "S7"


def _required_mapping(source: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = source.get(key)
    if not isinstance(value, Mapping):
        raise PlcConfigurationError(f"modbus_tcp.{key} must be a mapping")
    return value


def _required_int(
    source: Mapping[str, Any],
    key: str,
    *,
    minimum: int = 0,
    maximum: int = 0xFFFF,
) -> int:
    value = source.get(key)
    if isinstance(value, bool) or value is None:
        raise PlcConfigurationError(f"missing modbus_tcp setting: {key}")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise PlcConfigurationError(f"modbus_tcp.{key} must be an integer") from exc
    if not minimum <= parsed <= maximum:
        raise PlcConfigurationError(
            f"modbus_tcp.{key} must be in [{minimum}, {maximum}]"
        )
    return parsed


def _required_float(
    source: Mapping[str, Any],
    key: str,
    *,
    minimum: float = 0.0,
) -> float:
    value = source.get(key)
    if isinstance(value, bool) or value is None:
        raise PlcConfigurationError(f"missing modbus_tcp setting: {key}")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise PlcConfigurationError(f"modbus_tcp.{key} must be numeric") from exc
    if parsed <= minimum:
        raise PlcConfigurationError(f"modbus_tcp.{key} must be greater than {minimum}")
    return parsed


class ModbusTcpPlcClient(PlcClient):
    """LFD835 V1.0/V1.1 monitoring and V1.2 control in the 64-word window.

    Numeric addresses and bit positions are loaded exclusively from the PLC
    configuration.  The controller above this class only sees logical command
    and status models.
    """

    def __init__(
        self,
        config: Mapping[str, Any],
        *,
        transport_factory: Callable[..., Any] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = dict(config)
        host = self.config.get("host")
        if not isinstance(host, str) or not host.strip():
            raise PlcConfigurationError("missing modbus_tcp setting: host")
        self._host = host.strip()
        self._port = _required_int(self.config, "port", minimum=1, maximum=65535)
        self._unit_id = _required_int(self.config, "unit_id", minimum=0, maximum=247)
        self._timeout_seconds = (
            _required_int(self.config, "timeout_ms", minimum=1, maximum=300_000)
            / 1000.0
        )
        self._retries = _required_int(self.config, "retries", minimum=0, maximum=10)

        mapping = _required_mapping(self.config, "mapping")
        self._identity = _required_mapping(mapping, "identity")
        self._status_map = _required_mapping(mapping, "status")
        self._status_registers = dict(_required_mapping(self._status_map, "registers"))
        # Frozen protocol slots; upgrade old persisted read windows in memory,
        # without replacing the user's YAML or transport settings.
        for name, address in (("reset_ack_sequence", 21), ("reset_result", 22), ("fault_source_flags", 23),
                              ("ps1_feedback_flags", 28), ("ps1_status_sequence", 29),
                              ("ps1_power_cycle", 30), ("ps1_status_age_ms", 31)):
            self._status_registers.setdefault(name, address)
            if self._status_registers[name] != address:
                raise PlcConfigurationError(f"V1.2 {name} must use frozen HR{address}")
        self._status_bits = _required_mapping(self._status_map, "bits")
        self._command_map = _required_mapping(mapping, "command")
        self._command_registers = _required_mapping(self._command_map, "registers")
        self._command_bits = _required_mapping(self._command_map, "bits")
        self._scaling = _required_mapping(mapping, "scaling")
        self._compatibility = _required_mapping(mapping, "compatibility")

        self._status_start = _required_int(
            self._status_map, "start_address", maximum=63
        )
        self._status_count = _required_int(
            self._status_map, "count", minimum=1, maximum=64
        )
        if self._status_start != 0:
            raise PlcConfigurationError("V1.2 status must read one complete HR0..31 frame")
        self._status_count = 32
        if self._status_start + self._status_count > 64:
            raise PlcConfigurationError(
                "modbus_tcp status window must stay inside the 64-word PLC window"
            )

        self._validate_mapping()
        self._transport_factory = transport_factory or self._default_transport_factory
        self._clock = clock
        self._transport: Any | None = None
        self._connected = False
        self._lock = threading.RLock()
        self._last_sent_logical_sequence: int | None = None
        self._set_voltage_raw = 0
        self._set_current_raw = 0
        self._local_status_counter = 0
        self._protocol_version = 0

    @staticmethod
    def _default_transport_factory(host: str, **kwargs: Any) -> Any:
        try:
            from pymodbus.client import ModbusTcpClient
        except ImportError as exc:  # pragma: no cover - depends on deployment
            raise PlcConfigurationError(
                "pymodbus 3.8.6 is required for the modbus_tcp PLC driver"
            ) from exc
        return ModbusTcpClient(host, **kwargs)

    @property
    def connected(self) -> bool:
        with self._lock:
            return self._connected

    def connect(self) -> None:
        with self._lock:
            if self._connected:
                return
            try:
                transport = self._transport_factory(
                    self._host,
                    port=self._port,
                    timeout=self._timeout_seconds,
                    retries=self._retries,
                )
                if not transport.connect():
                    raise PlcCommunicationError(
                        f"cannot connect to PLC Modbus TCP server {self._host}:{self._port}"
                    )
                self._transport = transport
                self._connected = True
                registers = self._read_registers_locked()
                self._validate_identity(registers)
                self._protocol_version = self._register(registers, "protocol_version")
                self._update_setpoint_cache(registers)
                accepted = self._register(registers, "acknowledged_sequence")
                self._last_sent_logical_sequence = accepted
            except Exception as exc:
                self._close_transport_locked()
                if isinstance(exc, (PlcCommunicationError, PlcConfigurationError)):
                    raise
                raise PlcCommunicationError(
                    f"Modbus TCP connect failed: {exc}"
                ) from exc

    def disconnect(self) -> None:
        with self._lock:
            self._close_transport_locked()

    def read_status(self) -> PlcStatus:
        with self._lock:
            self._ensure_connected()
            try:
                registers = self._read_registers_locked()
                self._validate_identity(registers)
                self._protocol_version = self._register(registers, "protocol_version")
                self._update_setpoint_cache(registers)
                return self._decode_status(registers)
            except Exception as exc:
                if isinstance(exc, (PlcCommunicationError, PlcConfigurationError)):
                    raise
                raise PlcCommunicationError(
                    f"Modbus TCP status read failed: {exc}"
                ) from exc

    def write_command(self, command: PlcCommand) -> None:
        with self._lock:
            self._ensure_connected()
            if self._protocol_version != PROTOCOL_V12 and not command.all_stop and (command.main_enable or command.ps1_output_enable or any(command.channel_enable)):
                raise PlcConfigurationError("控制需配套PLC Host Protocol V1.2（0x0102）；旧协议仅可监控")
            heartbeat = command.heartbeat_counter & 0xFFFF
            control_address = self._command_register("control_key")
            heartbeat_address = self._command_register("heartbeat")
            if heartbeat_address != control_address + 1:
                raise PlcConfigurationError(
                    "control_key and heartbeat registers must be contiguous for FC16"
                )
            control_key = _required_int(self._identity, "control_key")
            self._write_registers_locked(
                control_address,
                [control_key, heartbeat],
                "write control key and heartbeat",
            )

            # Heartbeats reuse the current transaction sequence and must never
            # re-trigger reset or another PLC command.
            if command.command_sequence == self._last_sent_logical_sequence:
                return

            flags = self._encode_command_flags(command)
            flags_address = self._command_register("command_flags")
            set_voltage_address = self._command_register("set_voltage")
            set_current_address = self._command_register("set_current")
            if (
                set_voltage_address != flags_address + 1
                or set_current_address != flags_address + 2
            ):
                raise PlcConfigurationError(
                    "command_flags/set_voltage/set_current must be contiguous for FC16"
                )

            voltage_raw = self._set_voltage_raw
            current_raw = self._set_current_raw
            if command.set_voltage is not None:
                voltage_raw = self._encode_setpoint(
                    command.set_voltage,
                    _required_float(self._scaling, "voltage_units_per_count"),
                    "SetVoltage",
                )
            if command.set_current is not None:
                current_raw = self._encode_setpoint(
                    command.set_current,
                    _required_float(self._scaling, "current_units_per_count"),
                    "SetCurrent",
                )
            if command.apply_setpoints and (
                command.set_voltage is None or command.set_current is None
            ):
                raise PlcConfigurationError(
                    "ApplySetpoints requires SetVoltage and SetCurrent"
                )

            # The protocol requires payload first, then a separate FC06 write
            # to CommandSeq as the transaction commit point.
            self._write_registers_locked(
                flags_address,
                [flags, voltage_raw, current_raw],
                "write command payload",
            )
            raw_sequence = command.command_sequence & 0xFFFF
            self._write_register_locked(
                self._command_register("command_sequence"),
                raw_sequence,
                "commit command sequence",
            )
            self._last_sent_logical_sequence = command.command_sequence
            self._set_voltage_raw = voltage_raw
            self._set_current_raw = current_raw

    def _validate_mapping(self) -> None:
        required_status_registers = {
            "magic",
            "protocol_version",
            "status_flags",
            "channel_flags",
            "fault_code",
            "ps1_mb_status",
            "ps1_device_status",
            "ps1_manager_state",
            "ps1_output_voltage",
            "ps1_output_current",
            "ps1_set_voltage",
            "ps1_set_current",
            "voltage_start",
            "current_start",
            "accepted_command_flags",
            "acknowledged_sequence",
            "heartbeat_echo",
            "host_server_status",
            "host_server_events",
            "reset_ack_sequence",
            "reset_result",
            "fault_source_flags",
            "ps1_feedback_flags",
            "ps1_status_sequence",
            "ps1_power_cycle",
            "ps1_status_age_ms",
        }
        for name in required_status_registers:
            address = _required_int(self._status_registers, name, maximum=63)
            if (
                not self._status_start
                <= address
                < self._status_start + self._status_count
            ):
                raise PlcConfigurationError(
                    f"status register {name}={address} is outside the configured read window"
                )
        for start_name in ("voltage_start", "current_start"):
            address = _required_int(self._status_registers, start_name, maximum=63)
            if address + 3 >= self._status_start + self._status_count:
                raise PlcConfigurationError(
                    f"status register array {start_name} exceeds the read window"
                )

        required_status_bits = {
            "plc_ready",
            "host_connected",
            "main_ready",
            "ps1_comm_ready",
            "ps1_output_actual",
            "fault_latched",
            "ps1_comm_fault",
            "ps1_device_fault",
            "setpoint_applied",
            "setpoint_rejected",
            "ps1_comm_busy",
        }
        for name in required_status_bits:
            _required_int(self._status_bits, name, maximum=15)

        for name in (
            "control_key",
            "heartbeat",
            "command_sequence",
            "command_flags",
            "set_voltage",
            "set_current",
        ):
            _required_int(self._command_registers, name, maximum=63)
        for name in (
            "main_enable",
            "channel_1_enable",
            "channel_2_enable",
            "channel_3_enable",
            "channel_4_enable",
            "ps1_output_enable",
            "all_stop",
            "reset_fault",
            "apply_setpoints",
        ):
            _required_int(self._command_bits, name, maximum=15)

        _required_int(self._identity, "magic")
        _required_int(self._identity, "protocol_version")
        _required_int(self._identity, "control_key")
        _required_float(self._scaling, "voltage_units_per_count")
        _required_float(self._scaling, "current_units_per_count")
        _required_float(self._scaling, "max_channel_voltage")
        _required_float(self._scaling, "max_channel_current")
        policy = str(self._compatibility.get("analog_valid_policy") or "")
        if policy not in {"fresh_read_and_range", "strict_mask"}:
            raise PlcConfigurationError(
                "modbus_tcp.mapping.compatibility.analog_valid_policy must be "
                "fresh_read_and_range or strict_mask"
            )
        if policy == "strict_mask":
            _required_int(self._status_registers, "analog_valid_mask", maximum=63)

    def _decode_status(self, registers: list[int]) -> PlcStatus:
        status_flags = self._register(registers, "status_flags")
        version = self._register(registers, "protocol_version")
        v11 = version in {PROTOCOL_V11, PROTOCOL_V12}
        fault_sources = self._register(registers, "fault_source_flags") if v11 else 0
        channel_flags = self._register(registers, "channel_flags")
        accepted_flags = self._register(registers, "accepted_command_flags")
        voltage_scale = _required_float(self._scaling, "voltage_units_per_count")
        current_scale = _required_float(self._scaling, "current_units_per_count")
        # V1.0 and V1.1 both use 1 mV/count for VS1..VS4 (0..50 V).
        # V1.1 adds reset receipts and fault sources without changing units.
        voltage = tuple(
            self._register_offset(registers, "voltage_start", index) * voltage_scale
            for index in range(4)
        )
        current = tuple(
            self._register_offset(registers, "current_start", index) * current_scale
            for index in range(4)
        )
        power = tuple(v * a for v, a in zip(voltage, current, strict=True))
        analog_valid_mask = self._analog_valid_mask(registers, voltage, current)
        raw_ack = self._register(registers, "acknowledged_sequence")
        acknowledged_sequence = raw_ack
        if (
            self._last_sent_logical_sequence is not None
            and self._last_sent_logical_sequence & 0xFFFF == raw_ack
        ):
            acknowledged_sequence = self._last_sent_logical_sequence
        self._local_status_counter += 1

        main_feedback = self._optional_boolean(registers, "main_contactor_fb")
        physical_permit = self._optional_four_bits(registers, "physical_permit")
        all_stop = self._bit(accepted_flags, self._command_bit("all_stop"))
        ps1_request = (
            self._bit(accepted_flags, self._command_bit("ps1_output_enable"))
            and not all_stop
        )
        ps1_comm_fault = self._status_bit(status_flags, "ps1_comm_fault") or bool(fault_sources & FaultSource.PS1_COMM)
        ps1_device_fault = self._status_bit(status_flags, "ps1_device_fault") or bool(fault_sources & FaultSource.PS1_DEVICE)
        return PlcStatus(
            plc_ready=self._status_bit(status_flags, "plc_ready"),
            host_connected=self._status_bit(status_flags, "host_connected"),
            main_contactor_fb=main_feedback,
            main_ready=self._status_bit(status_flags, "main_ready"),
            physical_permit=physical_permit,
            channel_permit=tuple(self._bit(channel_flags, index) for index in range(4)),
            ps1_request=ps1_request,
            ps1_actual_output=self._status_bit(status_flags, "ps1_output_actual"),
            ps1_comm_ok=(
                self._status_bit(status_flags, "ps1_comm_ready") and not ps1_comm_fault
            ),
            ps1_fault=ps1_comm_fault or ps1_device_fault,
            ps1_comm_fault=ps1_comm_fault,
            ps1_device_fault=ps1_device_fault,
            fault_latched=self._status_bit(status_flags, "fault_latched") or bool(fault_sources & FaultSource.PLC_LATCHED),
            fault_code=self._register(registers, "fault_code"),
            voltage=voltage,  # type: ignore[arg-type]
            current=current,  # type: ignore[arg-type]
            power=power,  # type: ignore[arg-type]
            analog_valid_mask=analog_valid_mask,
            acknowledged_sequence=acknowledged_sequence,
            status_counter=self._local_status_counter,
            heartbeat_echo=self._register(registers, "heartbeat_echo"),
            accepted_command_flags=accepted_flags,
            protocol_magic=self._register(registers, "magic"),
            protocol_version=version,
            status_flags=status_flags,
            reset_ack_sequence=self._register(registers, "reset_ack_sequence") if v11 else 0,
            reset_result=self._register(registers, "reset_result") if v11 else ResetResult.IDLE,
            fault_source_flags=fault_sources,
            ps1_mb_status=self._register(registers, "ps1_mb_status"),
            ps1_device_status=self._register(registers, "ps1_device_status"),
            ps1_manager_state=self._register(registers, "ps1_manager_state"),
            ps1_output_voltage=(
                self._register(registers, "ps1_output_voltage") * voltage_scale
            ),
            ps1_output_current=(
                self._register(registers, "ps1_output_current") * current_scale
            ),
            ps1_set_voltage=(
                self._register(registers, "ps1_set_voltage") * voltage_scale
            ),
            ps1_set_current=(
                self._register(registers, "ps1_set_current") * current_scale
            ),
            setpoint_applied=self._status_bit(status_flags, "setpoint_applied"),
            setpoint_rejected=self._status_bit(status_flags, "setpoint_rejected"),
            ps1_comm_busy=self._status_bit(status_flags, "ps1_comm_busy"),
            host_server_status=self._register(registers, "host_server_status"),
            host_server_events=self._register(registers, "host_server_events"),
            ps1_feedback_flags=self._register(registers, "ps1_feedback_flags") if version == PROTOCOL_V12 else 0,
            ps1_status_sequence=self._register(registers, "ps1_status_sequence") if version == PROTOCOL_V12 else 0,
            ps1_power_cycle=self._register(registers, "ps1_power_cycle") if version == PROTOCOL_V12 else 0,
            ps1_status_age_ms=self._register(registers, "ps1_status_age_ms") if version == PROTOCOL_V12 else 65535,
            received_monotonic=self._clock(),
        )

    def _analog_valid_mask(
        self,
        registers: list[int],
        voltage: tuple[float, ...],
        current: tuple[float, ...],
    ) -> int:
        policy = str(self._compatibility.get("analog_valid_policy"))
        if policy == "strict_mask":
            mask = self._register(registers, "analog_valid_mask") & 0x0F
        else:
            # V1.0 has no sensor-valid bits.  A successful, fresh FC03 read is
            # considered transport-valid, then impossible engineering values
            # are rejected per channel.  The mapping table keeps the missing
            # PLC validity mask as an explicit TODO.
            mask = 0x0F
        # Older user YAML may still say 100 V. The confirmed VS transducers
        # measure 0..50 V; preserve stricter user limits, never widen this range.
        max_voltage = min(50.0, _required_float(self._scaling, "max_channel_voltage"))
        max_current = _required_float(self._scaling, "max_channel_current")
        for index, (value_v, value_a) in enumerate(zip(voltage, current, strict=True)):
            if not 0.0 <= value_v <= max_voltage or not 0.0 <= value_a <= max_current:
                mask &= ~(1 << index)
        return mask

    def _encode_command_flags(self, command: PlcCommand) -> int:
        if command.all_stop:
            return 1 << self._command_bit("all_stop")
        flags = 0
        if command.main_enable:
            flags |= 1 << self._command_bit("main_enable")
        for index, enabled in enumerate(command.channel_enable, start=1):
            if enabled:
                flags |= 1 << self._command_bit(f"channel_{index}_enable")
        if command.ps1_output_enable:
            flags |= 1 << self._command_bit("ps1_output_enable")
        if command.reset_fault_pulse:
            flags |= 1 << self._command_bit("reset_fault")
        if command.apply_setpoints:
            flags |= 1 << self._command_bit("apply_setpoints")
        return flags

    @staticmethod
    def _encode_setpoint(value: float, scale: float, name: str) -> int:
        numeric = float(value)
        if not math.isfinite(numeric) or numeric < 0.0:
            raise PlcConfigurationError(f"{name} must be a finite non-negative value")
        raw = round(numeric / scale)
        if not 0 <= raw <= 0xFFFF:
            raise PlcConfigurationError(
                f"{name}={numeric:g} exceeds the configured UINT16 scaling"
            )
        return raw

    def _read_registers_locked(self) -> list[int]:
        self._ensure_connected()
        assert self._transport is not None
        response = self._transport.read_holding_registers(
            self._status_start,
            count=self._status_count,
            slave=self._unit_id,
        )
        self._check_response(response, "read status registers")
        values = list(getattr(response, "registers", ()))
        if len(values) != self._status_count:
            raise PlcCommunicationError(
                f"PLC returned {len(values)} status words, expected {self._status_count}"
            )
        if any(
            isinstance(value, bool) or not 0 <= int(value) <= 0xFFFF for value in values
        ):
            raise PlcCommunicationError("PLC returned a non-UINT16 status word")
        return [int(value) for value in values]

    def _write_registers_locked(
        self, address: int, values: list[int], operation: str
    ) -> None:
        assert self._transport is not None
        try:
            response = self._transport.write_registers(
                address,
                values,
                slave=self._unit_id,
            )
        except Exception as exc:
            raise PlcCommunicationError(f"{operation} failed: {exc}") from exc
        self._check_response(response, operation)

    def _write_register_locked(self, address: int, value: int, operation: str) -> None:
        assert self._transport is not None
        try:
            response = self._transport.write_register(
                address,
                value,
                slave=self._unit_id,
            )
        except Exception as exc:
            raise PlcCommunicationError(f"{operation} failed: {exc}") from exc
        self._check_response(response, operation)

    @staticmethod
    def _check_response(response: Any, operation: str) -> None:
        if response is None or not hasattr(response, "isError"):
            raise PlcCommunicationError(f"{operation} returned an invalid response")
        if response.isError():
            raise PlcCommunicationError(f"{operation} failed: {response}")

    def _validate_identity(self, registers: list[int]) -> None:
        magic = self._register(registers, "magic")
        expected_magic = _required_int(self._identity, "magic")
        if magic != expected_magic:
            raise PlcCommunicationError(
                f"PLC protocol magic mismatch: got 0x{magic:04X}, "
                f"expected 0x{expected_magic:04X}"
            )
        version = self._register(registers, "protocol_version")
        if version not in {PROTOCOL_V10, PROTOCOL_V11, PROTOCOL_V12}:
            raise PlcCommunicationError(
                f"unsupported PLC protocol version: got 0x{version:04X}, "
                "未知协议不可控制；需配套0x0102 (V1.2)，0x0100/0x0101仅可监控"
            )

    def _update_setpoint_cache(self, registers: list[int]) -> None:
        self._set_voltage_raw = self._register(registers, "ps1_set_voltage")
        self._set_current_raw = self._register(registers, "ps1_set_current")

    def _register(self, values: list[int], name: str) -> int:
        address = _required_int(self._status_registers, name, maximum=63)
        return values[address - self._status_start]

    def _register_offset(self, values: list[int], name: str, offset: int) -> int:
        address = _required_int(self._status_registers, name, maximum=63) + offset
        return values[address - self._status_start]

    def _command_register(self, name: str) -> int:
        return _required_int(self._command_registers, name, maximum=63)

    def _command_bit(self, name: str) -> int:
        return _required_int(self._command_bits, name, maximum=15)

    def _status_bit(self, flags: int, name: str) -> bool:
        return self._bit(flags, _required_int(self._status_bits, name, maximum=15))

    @staticmethod
    def _bit(value: int, index: int) -> bool:
        return bool(value & (1 << index))

    def _optional_boolean(self, registers: list[int], name: str) -> bool | None:
        optional = self._compatibility.get(name)
        if optional is None:
            return None
        if not isinstance(optional, Mapping):
            raise PlcConfigurationError(
                f"modbus_tcp.mapping.compatibility.{name} must be null or a mapping"
            )
        address = _required_int(optional, "address", maximum=63)
        bit = _required_int(optional, "bit", maximum=15)
        if not self._status_start <= address < self._status_start + self._status_count:
            raise PlcConfigurationError(
                f"optional status {name} is outside read window"
            )
        return self._bit(registers[address - self._status_start], bit)

    def _optional_four_bits(
        self, registers: list[int], name: str
    ) -> tuple[bool | None, bool | None, bool | None, bool | None]:
        optional = self._compatibility.get(name)
        if optional is None:
            return (None, None, None, None)
        if not isinstance(optional, Mapping):
            raise PlcConfigurationError(
                f"modbus_tcp.mapping.compatibility.{name} must be null or a mapping"
            )
        address = _required_int(optional, "address", maximum=63)
        first_bit = _required_int(optional, "first_bit", maximum=12)
        if not self._status_start <= address < self._status_start + self._status_count:
            raise PlcConfigurationError(
                f"optional status {name} is outside read window"
            )
        flags = registers[address - self._status_start]
        return tuple(self._bit(flags, first_bit + index) for index in range(4))  # type: ignore[return-value]

    def _ensure_connected(self) -> None:
        if not self._connected or self._transport is None:
            raise PlcCommunicationError("PLC Modbus TCP transport is disconnected")

    def _close_transport_locked(self) -> None:
        transport = self._transport
        self._transport = None
        self._connected = False
        self._last_sent_logical_sequence = None
        if transport is not None:
            with suppress(Exception):
                transport.close()


def build_plc_client(driver: str, config: Mapping[str, Any]) -> PlcClient:
    if driver == "mock":
        return MockPlcClient()
    if driver == "s7":
        return S7PlcClient(config)
    if driver == "modbus_tcp":
        return ModbusTcpPlcClient(config)
    raise PlcConfigurationError(f"unsupported PLC driver: {driver}")


__all__ = [
    "MockPlcClient",
    "ModbusTcpPlcClient",
    "PlcClient",
    "PlcCommunicationError",
    "PlcConfigurationError",
    "S7PlcClient",
    "build_plc_client",
]
