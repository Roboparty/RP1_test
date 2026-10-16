from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from factory_hmi.plc.client import (
    ModbusTcpPlcClient,
    PlcCommunicationError,
    PlcConfigurationError,
)
from factory_hmi.plc.models import PlcCommand

CONFIG_PATH = (
    Path(__file__).resolve().parents[1] / "factory_hmi" / "config" / "plc_cabinet.yaml"
)


class _Response:
    def __init__(self, registers: list[int] | None = None, *, error: bool = False):
        self.registers = registers or []
        self.error = error

    def isError(self) -> bool:
        return self.error

    def __str__(self) -> str:
        return "fake Modbus error" if self.error else "fake Modbus response"


class _FakeTransport:
    def __init__(self, registers: list[int], *, connect_result: bool = True):
        self.registers = registers
        self.connect_result = connect_result
        self.closed = False
        self.read_calls: list[tuple[int, int, int]] = []
        self.multi_writes: list[tuple[int, list[int], int]] = []
        self.single_writes: list[tuple[int, int, int]] = []
        self.next_error = False

    def connect(self) -> bool:
        return self.connect_result

    def close(self) -> None:
        self.closed = True

    def read_holding_registers(
        self, address: int, *, count: int, slave: int
    ) -> _Response:
        self.read_calls.append((address, count, slave))
        if self.next_error:
            self.next_error = False
            return _Response(error=True)
        return _Response(list(self.registers[address : address + count]))

    def write_registers(
        self, address: int, values: list[int], *, slave: int
    ) -> _Response:
        self.multi_writes.append((address, list(values), slave))
        if self.next_error:
            self.next_error = False
            return _Response(error=True)
        for offset, value in enumerate(values):
            self.registers[address + offset] = value
        return _Response()

    def write_register(self, address: int, value: int, *, slave: int) -> _Response:
        self.single_writes.append((address, value, slave))
        if self.next_error:
            self.next_error = False
            return _Response(error=True)
        self.registers[address] = value
        if address == 34:
            self.registers[24] = value
            self.registers[20] = self.registers[35]
        return _Response()


def _protocol_config() -> dict[str, Any]:
    raw = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    return dict(raw["modbus_tcp"])


def _registers() -> list[int]:
    registers = [0] * 64
    registers[0] = 0x4C46
    registers[1] = 0x0100
    registers[2] = (
        (1 << 0) | (1 << 1) | (1 << 2) | (1 << 3) | (1 << 4) | (1 << 8) | (1 << 10)
    )
    registers[3] = 0b0101
    registers[4] = 0x0021
    registers[5] = 0x0002
    registers[6] = 0x0001
    registers[7] = 4
    registers[8] = 3000
    registers[9] = 500
    registers[10] = 5000
    registers[11] = 1000
    registers[12:16] = [24000, 24100, 23900, 24200]
    registers[16:20] = [1000, 2000, 3000, 4000]
    registers[20] = (1 << 0) | (1 << 1) | (1 << 3) | (1 << 5)
    registers[24] = 0xFFFF
    registers[25] = 9
    registers[26] = 0x7000
    registers[27] = 0b011
    return registers


def _client(
    transport: _FakeTransport,
) -> ModbusTcpPlcClient:
    return ModbusTcpPlcClient(
        _protocol_config(),
        transport_factory=lambda _host, **_kwargs: transport,
        clock=lambda: 123.5,
    )


def test_decodes_v1_status_without_inventing_missing_feedback() -> None:
    transport = _FakeTransport(_registers())
    client = _client(transport)
    client.connect()

    status = client.read_status()

    assert transport.read_calls == [(0, 32, 1), (0, 32, 1)]
    assert status.plc_ready is True
    assert status.host_connected is True
    assert status.main_ready is True
    assert status.main_contactor_fb is None
    assert status.physical_permit == (None, None, None, None)
    assert status.channel_permit == (True, False, True, False)
    assert status.ps1_request is True
    assert status.ps1_actual_output is True
    assert status.ps1_comm_ok is True
    assert status.ps1_fault is False
    assert status.voltage == pytest.approx((24.0, 24.1, 23.9, 24.2))
    assert status.current == pytest.approx((1.0, 2.0, 3.0, 4.0))
    assert status.power == pytest.approx((24.0, 48.2, 71.7, 96.8))
    assert status.analog_valid_mask == 0b1111
    assert status.acknowledged_sequence == 0xFFFF
    assert status.heartbeat_echo == 9
    assert status.ps1_output_voltage == pytest.approx(3.0)
    assert status.ps1_output_current == pytest.approx(0.5)
    assert status.setpoint_applied is True
    assert status.ps1_comm_busy is True


def test_command_writes_heartbeat_payload_then_sequence_and_unwraps_ack() -> None:
    transport = _FakeTransport(_registers())
    transport.registers[1] = 0x0102
    client = _client(transport)
    client.connect()
    command = PlcCommand(
        main_enable=True,
        channel_enable=(True, False, False, True),
        ps1_output_enable=True,
        reset_fault_pulse=True,
        heartbeat_counter=0x10001,
        command_sequence=0x10000,
    )

    client.write_command(command)

    expected_flags = (1 << 0) | (1 << 1) | (1 << 4) | (1 << 5) | (1 << 7)
    assert transport.multi_writes == [
        (32, [0x835A, 1], 1),
        (35, [expected_flags, 5000, 1000], 1),
    ]
    assert transport.single_writes == [(34, 0, 1)]
    assert client.read_status().acknowledged_sequence == 0x10000

    # Same sequence is heartbeat-only, even if the caller accidentally keeps
    # a pulse bit in its object.
    client.write_command(
        PlcCommand(
            reset_fault_pulse=True,
            heartbeat_counter=2,
            command_sequence=0x10000,
        )
    )
    assert transport.multi_writes[-1] == (32, [0x835A, 2], 1)
    assert len(transport.single_writes) == 1


def test_all_stop_bit_suppresses_every_enable_bit() -> None:
    transport = _FakeTransport(_registers())
    client = _client(transport)
    client.connect()

    client.write_command(
        PlcCommand(
            main_enable=True,
            channel_enable=(True, True, True, True),
            ps1_output_enable=True,
            all_stop=True,
            command_sequence=0x10000,
        )
    )

    assert transport.multi_writes[-1][1][0] == 1 << 6


def test_setpoint_command_encodes_engineering_units_and_apply_bit() -> None:
    transport = _FakeTransport(_registers())
    client = _client(transport)
    client.connect()

    client.write_command(
        PlcCommand(
            set_voltage=24.5,
            set_current=8.25,
            apply_setpoints=True,
            command_sequence=0x10000,
        )
    )

    flags, voltage_raw, current_raw = transport.multi_writes[-1][1]
    assert flags == 1 << 8
    assert voltage_raw == 24_500
    assert current_raw == 8_250
    assert transport.single_writes[-1] == (34, 0, 1)


def test_setpoint_command_rejects_values_not_encodable_as_uint16() -> None:
    client = _client(_FakeTransport(_registers()))
    client.connect()

    with pytest.raises(PlcConfigurationError, match="exceeds"):
        client.write_command(
            PlcCommand(
                set_voltage=80.0,
                set_current=1.0,
                apply_setpoints=True,
                command_sequence=0x10000,
            )
        )


def test_out_of_range_current_clears_only_affected_analog_valid_bit() -> None:
    registers = _registers()
    registers[17] = 60_000
    client = _client(_FakeTransport(registers))
    client.connect()

    status = client.read_status()

    assert status.analog_valid_mask == 0b1101
    assert status.to_dict()["Current"][1] is None


def test_bad_protocol_identity_closes_transport() -> None:
    registers = _registers()
    registers[0] = 0x1234
    transport = _FakeTransport(registers)
    client = _client(transport)

    with pytest.raises(PlcCommunicationError, match="magic mismatch"):
        client.connect()

    assert client.connected is False
    assert transport.closed is True


def test_modbus_error_is_promoted_to_communication_error() -> None:
    transport = _FakeTransport(_registers())
    client = _client(transport)
    client.connect()
    transport.next_error = True

    with pytest.raises(PlcCommunicationError, match="read status registers"):
        client.read_status()


def test_incomplete_mapping_is_rejected_before_network_access() -> None:
    config = _protocol_config()
    config["mapping"] = dict(config["mapping"])
    config["mapping"]["status"] = None

    with pytest.raises(PlcConfigurationError, match="status must be a mapping"):
        ModbusTcpPlcClient(config)


@pytest.mark.parametrize("version", [0x0100, 0x0101])
@pytest.mark.parametrize("raw,voltage,valid", [
    (0, 0.0, True), (1, 0.001, True), (10000, 10.0, True),
    (49999, 49.999, True), (50000, 50.0, True),
    (50001, 50.001, False), (65535, 65.535, False),
])
def test_branch_voltage_1mv_50v_boundary_in_both_protocols(version, raw, voltage, valid):
    registers = _registers()
    registers[1] = version
    registers[12:16] = [raw] * 4
    registers[16:20] = [50000] * 4
    client = _client(_FakeTransport(registers))
    client.connect()
    status = client.read_status()
    assert status.voltage == pytest.approx((voltage,) * 4)
    assert status.current == pytest.approx((50.0,) * 4)
    assert status.power == pytest.approx((voltage * 50.0,) * 4)
    assert status.analog_valid_mask == (0b1111 if valid else 0)
    assert status.ps1_output_voltage == pytest.approx(3.0)
    assert status.ps1_set_voltage == pytest.approx(5.0)


def test_v11_decodes_reset_receipt_and_independent_faults_with_zero_code():
    registers = _registers()
    registers[1] = 0x0101
    registers[2] = (1 << 6) | (1 << 7)
    registers[4] = 0
    registers[21:24] = [0xFFFF, 2, 6]
    client = _client(_FakeTransport(registers))
    client.connect()
    snapshot = client.read_status().to_dict()
    assert snapshot["ResetAckSeq"] == 0xFFFF
    assert snapshot["ResetResult"] == 2
    assert snapshot["FaultSourceFlags"] == 6
    assert snapshot["FaultLatched"] is False
    assert snapshot["PS1CommFault"] is True
    assert snapshot["PS1DeviceFault"] is True
    assert snapshot["PS1Fault"] is True
    assert snapshot["FaultCode"] == 0


def test_legacy_words_21_to_23_are_not_interpreted_as_v11_receipts():
    registers = _registers()
    registers[21:24] = [1234, 2, 7]
    client = _client(_FakeTransport(registers))
    client.connect()
    snapshot = client.read_status().to_dict()
    assert snapshot["ResetResultSupported"] is False
    assert snapshot["ResetAckSeq"] is None
    assert snapshot["ResetResult"] is None
    assert snapshot["FaultSourceFlags"] == 0


def test_persisted_v10_mapping_can_read_new_fixed_slots_without_config_mutation():
    config = _protocol_config()
    config["mapping"]["identity"]["protocol_version"] = 0x0100
    for name in ("reset_ack_sequence", "reset_result", "fault_source_flags"):
        config["mapping"]["status"]["registers"].pop(name)
    registers = _registers()
    registers[1] = 0x0101
    registers[21:24] = [10, 2, 0]
    client = ModbusTcpPlcClient(config, transport_factory=lambda *args, **kwargs: _FakeTransport(registers))
    client.connect()
    assert client.read_status().reset_ack_sequence == 10
    assert "reset_ack_sequence" not in config["mapping"]["status"]["registers"]


def test_unknown_protocol_version_is_rejected_before_commands():
    registers = _registers()
    registers[1] = 0x0103
    transport = _FakeTransport(registers)
    client = _client(transport)
    with pytest.raises(PlcCommunicationError, match="unsupported PLC protocol version"):
        client.connect()
    assert transport.closed
    assert transport.single_writes == transport.multi_writes == []


@pytest.mark.parametrize("version", [0x0100, 0x0101])
def test_legacy_100v_yaml_cannot_accept_measurements_beyond_confirmed_50v_range(version):
    config = _protocol_config()
    config["mapping"]["scaling"]["max_channel_voltage"] = 100.0
    registers = _registers()
    registers[1] = version
    registers[12:16] = [50000, 50001, 60000, 65535]
    client = ModbusTcpPlcClient(config, transport_factory=lambda *args, **kwargs: _FakeTransport(registers))
    client.connect()
    status = client.read_status()
    assert status.voltage == pytest.approx((50.0, 50.001, 60.0, 65.535))
    assert status.analog_valid_mask == 0b0001
    assert config["mapping"]["scaling"]["max_channel_voltage"] == 100.0
