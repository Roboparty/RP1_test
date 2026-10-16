"""Regression tests for startup budget cancellation; virtual time, in-memory PLC only.

No TCP/CAN endpoints or hardware are used. The mock reports fresh PS1 frames and
receives normal heartbeats throughout the elapsed run; healthy operation must
not produce an OFF/AllStop transaction when the former startup budget expires.
"""
from dataclasses import replace

import pytest

from factory_hmi.plc.client import MockPlcClient
from factory_hmi.plc.config import PlcSetpointConfig, PlcTimingConfig
from factory_hmi.plc.models import CabinetPhase, FeedbackState
from factory_hmi.plc.state_machine import PlcCabinetController


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


def setup(*, client_type=MockPlcClient, **client_options):
    clock = Clock()
    client = client_type(clock=clock, **client_options)
    controller = PlcCabinetController(
        client,
        PlcTimingConfig(),
        PlcSetpointConfig(True, 0.001, 60.0, 0.001, 60.0),
        clock=clock,
    )
    controller.connect(user="offline-regression")
    return clock, client, controller


def tick_until(clock, controller, phase, *, limit=60):
    for _ in range(limit):
        controller.tick()
        if controller.phase == phase:
            return
        clock.now += 0.2
    raise AssertionError(f"expected {phase}, got {controller.phase}: {controller.last_error}")


def keep_healthy_through_deadline(clock, client, controller, deadline):
    """Run 10 minutes beyond the former deadline with fresh feedback and HB."""
    before = len(client.command_history)
    expected_channels = controller.command.channel_enable
    expected_start_feedback = replace(controller.feedback["Start"])
    last_heartbeat = clock.now
    while clock.now < deadline + 600.0:
        clock.now += 0.2
        controller.tick()
        assert controller.phase == CabinetPhase.RUNNING, controller.last_error
        if clock.now - last_heartbeat >= 0.5:
            controller.heartbeat()
            last_heartbeat = clock.now
    assert controller.feedback["Start"] == expected_start_feedback
    after = client.command_history[before:]
    assert after, "normal heartbeat writes must have occurred during the run"
    assert all(
        not command.all_stop
        and command.main_enable
        and command.ps1_output_enable
        and command.channel_enable == expected_channels
        for command in after
    ), "a healthy completed run must not receive OFF/AllStop after its old deadline"
    assert client.last_command.heartbeat_counter > 1


@pytest.mark.parametrize("channels, physical, expected_channels, start_state", [
    ((True, True, True, True), (True, True, True, True),
     (True, True, True, True), FeedbackState.CONFIRMED),
    ((True, True, True, True), (True, False, True, True),
     (True, False, True, True), FeedbackState.CONFIRMED),
    ((False, False, False, False), (True, True, True, True),
     (False, False, False, False), FeedbackState.CONFIRMED),
    # Existing semantics retain the PS1/main circuit when all physical permits
    # are absent, but report Start rejected. Its start transaction is still over.
    ((True, True, True, True), (False, False, False, False),
     (False, False, False, False), FeedbackState.REJECTED),
], ids=["all-permitted", "one-physical-permit-missing", "supply-only", "all-physical-permits-missing"])
def test_completed_start_does_not_time_out_healthy_running(
    channels, physical, expected_channels, start_state
):
    clock, client, controller = setup(physical_permit=physical)
    assert controller.request_start(
        channels, user="offline-regression", voltage=3.0, current=0.5
    )
    deadline = controller._startup_deadline
    assert controller.startup_budget_ms == 70400
    tick_until(clock, controller, CabinetPhase.RUNNING)
    assert controller.command.channel_enable == expected_channels
    assert controller.feedback["Start"].state == start_state
    assert not controller._start_requested, "RUNNING must finish the startup transaction"
    assert controller._startup_deadline == 0.0, "completed startup must retire its absolute deadline"
    keep_healthy_through_deadline(clock, client, controller, deadline)


class MissingFinalChannelPermitMock(MockPlcClient):
    """Physical permit unknown, but PLC rejects final channel 2 permission."""
    def read_status(self):
        status = super().read_status()
        if self.last_command.channel_enable[1]:
            status = replace(status, channel_permit=(status.channel_permit[0], False,
                                                     status.channel_permit[2], status.channel_permit[3]))
        return status


def test_partial_final_permit_timeout_keeps_confirmed_channels_running():
    clock, client, controller = setup(
        client_type=MissingFinalChannelPermitMock,
        physical_permit=(None, None, None, None),
    )
    assert controller.request_start(user="offline-regression", voltage=3.0, current=0.5)
    deadline = controller._startup_deadline
    tick_until(clock, controller, CabinetPhase.RUNNING)
    assert controller.command.channel_enable == (True, False, True, True)
    assert any(
        entry.field == "ChannelEnable2"
        and entry.confirmation == FeedbackState.REJECTED
        and "PLC final ChannelFlags" in entry.detail
        for entry in controller.operation_log
    )
    assert controller.feedback["Start"].state == FeedbackState.CONFIRMED
    assert not controller._start_requested
    assert controller._startup_deadline == 0.0
    keep_healthy_through_deadline(clock, client, controller, deadline)


@pytest.mark.parametrize("fault", [
    dict(ps1_comm_fault=True, ps1_comm_ok=False, ps1_mb_status=0xF001),
    dict(host_connected=False),
    dict(ps1_status_age_ms=5000),
    dict(ps1_actual_output=False),
], ids=["ps1-communication-fault", "host-heartbeat-loss", "ps1-feedback-expired", "ps1-output-lost"])
def test_completed_start_keeps_runtime_guards_after_old_deadline(fault):
    clock, client, controller = setup()
    assert controller.request_start(user="offline-regression", voltage=3.0, current=0.5)
    deadline = controller._startup_deadline
    tick_until(clock, controller, CabinetPhase.RUNNING)
    keep_healthy_through_deadline(clock, client, controller, deadline)
    client.set_status(**fault)
    controller.tick()
    assert controller.phase == CabinetPhase.FAULT
    assert controller.last_error
    assert "总时间预算耗尽" not in controller.last_error
    assert client.last_command.all_stop
    assert not client.last_command.main_enable
    assert not client.last_command.ps1_output_enable
    assert not any(client.last_command.channel_enable)


def test_unfinished_start_retains_absolute_startup_deadline():
    clock, client, controller = setup(auto_main_ready=False)
    assert controller.request_start(user="offline-regression", voltage=3.0, current=0.5)
    deadline = controller._startup_deadline
    tick_until(clock, controller, CabinetPhase.WAIT_MAIN_READY)
    assert controller._start_requested
    assert controller.feedback["Start"].state == FeedbackState.PENDING
    clock.now = deadline
    controller.tick()
    assert controller.phase == CabinetPhase.FAULT
    assert "总时间预算耗尽" in controller.last_error
    assert controller.feedback["Start"].state == FeedbackState.TIMEOUT
    assert client.last_command.all_stop
    assert not client.last_command.main_enable
    assert not client.last_command.ps1_output_enable
    assert not any(client.last_command.channel_enable)


def test_unfinished_start_retains_individual_phase_deadline():
    clock, client, controller = setup(auto_main_ready=False)
    assert controller.request_start(user="offline-regression", voltage=3.0, current=0.5)
    tick_until(clock, controller, CabinetPhase.WAIT_MAIN_READY)
    assert controller._phase_deadline < controller._startup_deadline
    clock.now = controller._phase_deadline
    controller.tick()
    assert controller.phase == CabinetPhase.FAULT
    assert "KM0 feedback/MainReady timeout" in controller.last_error
    assert client.last_command.all_stop
    assert not client.last_command.main_enable
    assert not client.last_command.ps1_output_enable
    assert not any(client.last_command.channel_enable)
