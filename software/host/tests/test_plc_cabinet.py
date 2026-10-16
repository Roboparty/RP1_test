from __future__ import annotations

import time

from factory_hmi.plc.client import MockPlcClient
from factory_hmi.plc.config import PlcSetpointConfig, PlcTimingConfig
from factory_hmi.plc.models import CabinetPhase
from factory_hmi.plc.state_machine import PlcCabinetController


def _controller(client: MockPlcClient) -> PlcCabinetController:
    controller = PlcCabinetController(
        client,
        PlcTimingConfig(
            heartbeat_ms=500,
            poll_ms=100,
            stale_timeout_ms=60_000,
            command_timeout_ms=100,
            km0_timeout_ms=100,
            ps1_timeout_ms=100,
            reconnect_ms=100,
            trend_retention_seconds=60,
        ),
    )
    controller.connect(user="tester")
    return controller


def _setpoint_controller(client: MockPlcClient) -> PlcCabinetController:
    controller = PlcCabinetController(
        client,
        PlcTimingConfig(
            heartbeat_ms=500,
            poll_ms=100,
            stale_timeout_ms=60_000,
            command_timeout_ms=100,
            km0_timeout_ms=100,
            ps1_timeout_ms=100,
            reconnect_ms=100,
            trend_retention_seconds=60,
        ),
        PlcSetpointConfig(
            enabled=True,
            voltage_min_v=0.0,
            voltage_max_v=65.535,
            current_min_a=0.0,
            current_max_a=50.0,
            step=0.001,
        ),
    )
    controller.connect(user="tester")
    return controller


def _tick_until(
    controller: PlcCabinetController,
    phase: CabinetPhase,
    *,
    limit: int = 20,
) -> None:
    for _ in range(limit):
        controller.tick()
        if controller.phase == phase:
            return
    raise AssertionError(f"did not reach {phase}; current={controller.phase}")


def test_normal_start_waits_for_main_then_ps1_then_channels() -> None:
    client = MockPlcClient()
    controller = _controller(client)

    assert controller.request_start(user="alice")
    _tick_until(controller, CabinetPhase.RUNNING)

    changed = [
        command for command in client.command_history if command.command_sequence
    ]
    main_index = next(i for i, command in enumerate(changed) if command.main_enable)
    ps1_index = next(
        i for i, command in enumerate(changed) if command.ps1_output_enable
    )
    channel_index = next(
        i for i, command in enumerate(changed) if any(command.channel_enable)
    )
    assert main_index < ps1_index < channel_index
    assert controller.status is not None
    assert all(controller.status.channel_permit)
    assert controller.feedback["Start"].state.value == "confirmed"


def test_start_without_selected_channels_powers_supply_only() -> None:
    client = MockPlcClient()
    controller = _setpoint_controller(client)

    assert controller.request_start(
        (False, False, False, False),
        user="alice",
        voltage=3.0,
        current=0.5,
    )
    _tick_until(controller, CabinetPhase.RUNNING)

    assert controller.status is not None
    assert controller.status.main_ready
    assert controller.status.ps1_actual_output
    assert not any(controller.status.channel_permit)
    assert controller.feedback["Start"].state.value == "confirmed"
    assert "no channels selected" in controller.feedback["Start"].detail


def test_normal_start_applies_setpoints_before_ps1_output() -> None:
    client = MockPlcClient()
    controller = _setpoint_controller(client)
    before = len(client.command_history)

    assert controller.request_start(
        user="alice",
        voltage=3.0,
        current=0.5,
    )
    _tick_until(controller, CabinetPhase.RUNNING)

    changed = client.command_history[before:]
    stop_index = next(i for i, command in enumerate(changed) if command.all_stop)
    clear_index = next(
        i
        for i, command in enumerate(changed[stop_index + 1 :], stop_index + 1)
        if not command.all_stop
        and not command.main_enable
        and not command.ps1_output_enable
        and not any(command.channel_enable)
    )
    main_index = next(i for i, command in enumerate(changed) if command.main_enable)
    explicit_stop_index = next(
        i
        for i, command in enumerate(changed[main_index + 1 :], main_index + 1)
        if command.main_enable
        and not command.ps1_output_enable
        and not command.apply_setpoints
    )
    setpoint_index = next(
        i for i, command in enumerate(changed) if command.apply_setpoints
    )
    output_index = next(
        i for i, command in enumerate(changed) if command.ps1_output_enable
    )
    channel_index = next(
        i for i, command in enumerate(changed) if any(command.channel_enable)
    )
    assert (
        stop_index
        < clear_index
        < main_index
        < explicit_stop_index
        < setpoint_index
        < output_index
        < channel_index
    )
    assert controller.status is not None
    assert controller.status.ps1_set_voltage == 3.0
    assert controller.status.ps1_set_current == 0.5


def test_repeated_start_rearms_stop_and_skips_matching_setpoint_write() -> None:
    client = MockPlcClient()
    controller = _setpoint_controller(client)

    assert controller.request_start(user="alice", voltage=3.0, current=0.5)
    _tick_until(controller, CabinetPhase.RUNNING)
    assert controller.request_stop(user="alice")
    _tick_until(controller, CabinetPhase.OFF)
    before = len(client.command_history)

    assert controller.request_start(user="alice", voltage=3.0, current=0.5)
    _tick_until(controller, CabinetPhase.RUNNING)

    second_start = client.command_history[before:]
    assert second_start[0].all_stop is True
    assert not any(command.apply_setpoints for command in second_start)
    assert controller.feedback["Setpoints"].state.value == "confirmed"
    assert "already matches" in controller.feedback["Setpoints"].detail
    assert any(
        entry.field == "Setpoints" and "already matches" in entry.detail
        for entry in controller.operation_log
    )


def test_stale_setpoint_rejection_is_not_applied_to_new_transaction() -> None:
    class StickyRejectMock(MockPlcClient):
        def write_command(self, command):  # type: ignore[no-untyped-def]
            super().write_command(command)
            if command.apply_setpoints:
                self.set_status(setpoint_applied=False, setpoint_rejected=True)

    client = StickyRejectMock()
    controller = _setpoint_controller(client)
    client.set_status(setpoint_rejected=True)
    controller.tick()

    assert controller.request_setpoints(3.0, 0.5, user="alice")
    controller.tick()
    assert controller.feedback["Setpoints"].state.value == "pending"

    client.set_status(setpoint_applied=True, setpoint_rejected=False)
    controller.tick()
    assert controller.feedback["Setpoints"].state.value == "confirmed"


def test_normal_stop_is_channels_then_ps1_then_main() -> None:
    client = MockPlcClient()
    controller = _controller(client)
    controller.request_start(user="alice")
    _tick_until(controller, CabinetPhase.RUNNING)
    before = len(client.command_history)

    assert controller.request_stop(user="alice")
    _tick_until(controller, CabinetPhase.OFF)

    shutdown = client.command_history[before:]
    channel_off = next(
        i for i, item in enumerate(shutdown) if not any(item.channel_enable)
    )
    ps1_off = next(
        i
        for i, item in enumerate(shutdown[channel_off + 1 :], channel_off + 1)
        if not item.ps1_output_enable
    )
    main_off = next(
        i
        for i, item in enumerate(shutdown[ps1_off + 1 :], ps1_off + 1)
        if not item.main_enable
    )
    assert channel_off < ps1_off < main_off
    assert controller.feedback["Stop"].state.value == "confirmed"


def test_km0_timeout_enters_fault_and_all_stop() -> None:
    client = MockPlcClient(auto_main_ready=False)
    controller = _controller(client)
    controller.request_start(user="alice")
    _tick_until(controller, CabinetPhase.WAIT_MAIN_READY)

    controller.tick(time.monotonic() + 1.0)

    assert controller.phase == CabinetPhase.FAULT
    assert "KM0" in controller.last_error
    assert client.last_command.all_stop
    assert controller.feedback["MainEnable"].state.value == "timeout"


def test_ps1_communication_timeout_enters_fault() -> None:
    client = MockPlcClient()
    controller = _controller(client)
    client.set_status(ps1_comm_ok=False)
    controller.request_start(user="alice")
    _tick_until(controller, CabinetPhase.WAIT_PS1_COMM)

    # V1.5 has a fixed 5s MainReady delay before attempting PS1 I/O.
    controller.tick(controller._phase_deadline + 0.001)

    assert controller.phase == CabinetPhase.FAULT
    assert controller.last_error == "PS1 communication timeout"
    assert client.last_command.all_stop


def test_disconnect_and_reconnect_never_restore_running_outputs() -> None:
    client = MockPlcClient()
    controller = _controller(client)
    controller.request_start(user="alice")
    _tick_until(controller, CabinetPhase.RUNNING)

    client.disconnect()
    controller.tick()
    assert controller.phase == CabinetPhase.COMM_LOST
    assert not controller.command.main_enable
    assert not controller.command.ps1_output_enable

    client.connect()
    controller.tick()
    assert controller.phase == CabinetPhase.OFF
    assert not client.last_command.main_enable
    assert not client.last_command.ps1_output_enable
    assert not any(client.last_command.channel_enable)


def test_missing_physical_permit_rejects_only_affected_channel() -> None:
    client = MockPlcClient(physical_permit=(True, False, True, True))
    controller = _controller(client)
    controller.request_start(user="alice")
    _tick_until(controller, CabinetPhase.RUNNING)

    assert controller.command.channel_enable == (True, False, True, True)
    assert controller.feedback["ChannelEnable2"].state.value == "rejected"
    assert controller.status is not None
    assert controller.status.channel_permit == (True, False, True, True)


def test_protocol_without_raw_physical_permits_uses_final_channel_feedback() -> None:
    client = MockPlcClient(physical_permit=(None, None, None, None))
    controller = _controller(client)

    assert controller.request_start(user="alice")
    _tick_until(controller, CabinetPhase.RUNNING)

    assert controller.command.channel_enable == (True, True, True, True)
    assert controller.status is not None
    assert controller.status.physical_permit == (None, None, None, None)
    assert controller.status.channel_permit == (True, True, True, True)


def test_start_is_rejected_until_plc_confirms_host_connected() -> None:
    client = MockPlcClient()
    controller = _controller(client)
    client.set_status(host_connected=False)
    controller.tick()

    assert not controller.request_start(user="alice")
    assert controller.feedback["Start"].state.value == "rejected"
    assert "HostConnected" in controller.feedback["Start"].detail


def test_invalid_analog_channel_is_not_exposed_as_live_value() -> None:
    client = MockPlcClient()
    controller = _controller(client)
    client.set_status(
        analog_valid_mask=0b1101,
        voltage=(24.0, 99.0, 24.1, 23.9),
        current=(2.0, 49.0, 2.1, 1.9),
        power=(48.0, 4851.0, 50.61, 45.41),
    )
    controller.tick()

    status = controller.snapshot()["status"]
    assert status["AnalogValidMask"] == 0b1101
    assert status["Voltage"] == [24.0, None, 24.1, 23.9]
    assert status["Current"][1] is None
    assert status["Power"][1] is None


def test_reset_fault_is_single_write_pulse() -> None:
    client = MockPlcClient()
    controller = _controller(client)
    client.set_status(fault_latched=True, fault_code=7)

    assert controller.request_reset_fault(user="alice")
    assert client.command_history[-1].reset_fault_pulse is True
    assert controller.command.reset_fault_pulse is False
    controller.tick()
    controller.heartbeat()
    assert client.command_history[-1].reset_fault_pulse is False


def test_stale_data_hides_analog_values_and_blocks_start() -> None:
    client = MockPlcClient()
    controller = _controller(client)
    assert controller.status is not None
    client.refresh_on_read = False
    stale_at = controller.status.received_monotonic + 61.0

    controller.tick(stale_at)

    assert controller.phase == CabinetPhase.STALE
    assert controller.snapshot()["status"]["Voltage"] == [None] * 4
    assert not controller.request_start(user="alice")
    assert controller.feedback["Start"].state.value == "rejected"


def test_ps1_setpoints_are_applied_only_while_off_and_confirmed_by_plc() -> None:
    client = MockPlcClient()
    controller = _setpoint_controller(client)

    assert controller.request_setpoints(24.5, 8.25, user="alice")
    assert client.command_history[-1].apply_setpoints is True
    assert controller.command.apply_setpoints is False
    controller.tick()

    assert controller.feedback["Setpoints"].state.value == "confirmed"
    assert controller.status is not None
    assert controller.status.ps1_set_voltage == 24.5
    assert controller.status.ps1_set_current == 8.25
    assert controller.operation_log[-1].field == "Setpoints"
    assert controller.operation_log[-1].confirmation.value == "confirmed"

    controller.request_start(user="alice")
    _tick_until(controller, CabinetPhase.RUNNING)
    assert not controller.request_setpoints(20.0, 5.0, user="alice")
    assert controller.feedback["Setpoints"].state.value == "rejected"


def test_ps1_setpoints_reject_values_outside_configured_limits() -> None:
    controller = _setpoint_controller(MockPlcClient())

    assert not controller.request_setpoints(80.0, 5.0, user="alice")
    assert "voltage" in controller.feedback["Setpoints"].detail
    assert not controller.request_setpoints(24.0, 55.0, user="alice")
    assert "current" in controller.feedback["Setpoints"].detail
