"""Frozen V1.2 contract, virtual clocks and command history; no device I/O."""
from dataclasses import replace

import pytest

from factory_hmi.plc.client import MockPlcClient, ModbusTcpPlcClient, PlcConfigurationError
from factory_hmi.plc.config import PlcSetpointConfig, PlcTimingConfig
from factory_hmi.plc.freshness import Ps1FeedbackTracker
from factory_hmi.plc.models import CabinetPhase, PlcStatus, PROTOCOL_V12, ResetResult
from factory_hmi.plc.state_machine import PlcCabinetController
from test_plc_modbus_tcp import _FakeTransport, _protocol_config, _registers


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


def setup(*, running=False):
    clock = Clock()
    client = MockPlcClient(clock=clock)
    controller = PlcCabinetController(client, PlcTimingConfig(),
        PlcSetpointConfig(True, 0.001, 60.0, 0.001, 60.0), clock=clock)
    controller.connect(user="offline")
    if running:
        assert controller.request_start(user="offline", voltage=3.0, current=0.5)
        for _ in range(30):
            controller.tick()
            if controller.phase == CabinetPhase.RUNNING:
                break
        assert controller.phase == CabinetPhase.RUNNING
    return clock, client, controller


def sample(**changes):
    return replace(PlcStatus(protocol_version=PROTOCOL_V12, main_ready=True,
        ps1_feedback_flags=0xCF, ps1_status_sequence=10, ps1_power_cycle=1,
        ps1_status_age_ms=0), **changes)


def test_whole_32_word_frame_and_retained_v11_features_and_50v():
    words = _registers()
    words[1] = PROTOCOL_V12
    words[12:16] = [50000, 50000, 50000, 50000]
    words[21:24] = [31, ResetResult.SUCCEEDED, 7]
    words[28:32] = [0xCF, 65535, 8, 4999]
    transport = _FakeTransport(words)
    client = ModbusTcpPlcClient(_protocol_config(), transport_factory=lambda *a, **k: transport)
    client.connect()
    status = client.read_status()
    assert transport.read_calls == [(0, 32, 1)] * 2
    assert status.voltage == (50.0,) * 4
    assert status.ps1_status_sequence == 65535
    assert status.ps1_power_cycle == 8
    assert status.ps1_status_age_ms == 4999
    assert status.reset_results_supported and status.reset_ack_sequence == 31
    assert status.reset_result == ResetResult.SUCCEEDED and status.fault_source_flags == 7


def test_persisted_28_word_yaml_is_upgraded_in_memory_only():
    config = _protocol_config()
    config["mapping"]["status"]["count"] = 28
    words = _registers()
    words[1] = PROTOCOL_V12
    transport = _FakeTransport(words)
    client = ModbusTcpPlcClient(config, transport_factory=lambda *a, **k: transport)
    client.connect()
    assert transport.read_calls == [(0, 32, 1)]
    assert config["mapping"]["status"]["count"] == 28


@pytest.mark.parametrize("version", [0x0100, 0x0101, 0x0103])
def test_control_connect_rejects_old_or_unknown_before_any_write(version):
    client = MockPlcClient()
    client.set_status(protocol_version=version)
    controller = PlcCabinetController(client)
    with pytest.raises(PlcConfigurationError, match="V1.2"):
        controller.connect(user="offline")
    assert client.command_history == []


@pytest.mark.parametrize("fault", [dict(fault_latched=True), dict(ps1_comm_fault=True),
    dict(ps1_device_fault=True), dict(fault_source_flags=8), dict(fault_source_flags=7)])
def test_same_frame_setpoint_success_and_fault_never_sends_on(fault):
    _, client, controller = setup()
    assert controller.request_start(user="offline", voltage=3.0, current=0.5)
    for _ in range(30):
        controller.tick()
        if controller._pending and controller._pending.controls == ("Setpoints",):
            break
    assert controller._pending.controls == ("Setpoints",)
    before = len(client.command_history)
    client.set_status(setpoint_applied=True, setpoint_rejected=False, **fault)
    controller.tick()
    assert controller.phase == CabinetPhase.FAULT
    assert not any(c.ps1_output_enable or any(c.channel_enable) for c in client.command_history[before:])
    assert client.last_command.all_stop and not client.last_command.main_enable
    assert controller.feedback["Setpoints"].state.value != "confirmed"


@pytest.mark.parametrize("flags", [0xCE, 0xCD, 0xCB, 0xC7, 0x8F, 0x4F, 0xDF, 0xEF, 0x1CF])
def test_final_on_entry_requires_every_health_flag_and_no_lock(flags):
    _, client, controller = setup(running=True)
    controller.status = replace(controller.status, ps1_feedback_flags=flags)
    before = len(client.command_history)
    controller._start_ps1_output("offline")
    assert not any(c.ps1_output_enable or any(c.channel_enable) for c in client.command_history[before:])
    assert client.last_command.all_stop


@pytest.mark.parametrize("changes", [dict(ps1_status_age_ms=5000),
    dict(ps1_status_age_ms=65535), dict(ps1_feedback_flags=0xDF),
    dict(ps1_feedback_flags=0xEF), dict(ps1_feedback_flags=0x8F),
    dict(ps1_feedback_flags=0x4F), dict(host_connected=False)])
def test_feedback_expiry_lock_or_missing_samples_withdraws_power(changes):
    _, client, controller = setup(running=True)
    client.set_status(**changes)
    controller.tick()
    assert client.last_command.all_stop
    assert not client.last_command.main_enable
    assert not any(client.last_command.channel_enable)


def test_repeat_status_sequence_cannot_extend_5s_deadline_even_with_live_tcp():
    clock, client, controller = setup(running=True)
    client.refresh_ps1_on_read = False
    client.set_status(ps1_status_age_ms=0)
    controller.tick()
    clock.now += 4.999
    controller.tick()
    assert controller.phase == CabinetPhase.RUNNING
    clock.now += 0.001
    controller.tick()
    assert controller.phase == CabinetPhase.FAULT
    assert client.last_command.all_stop
    assert controller.snapshot()["status"]["PS1ActualOutput"] is None


def test_status_counter_wrap_is_progress_but_backwards_reset_invalidates():
    tracker = Ps1FeedbackTracker()
    tracker.observe(sample(ps1_status_sequence=65535), 0.0)
    assert not tracker.fresh(sample(), 0.0)
    tracker.observe(sample(ps1_status_sequence=0), 0.1)
    assert tracker.fresh(sample(ps1_status_sequence=0), 0.1)
    tracker.observe(sample(ps1_status_sequence=200), 0.2)
    tracker.observe(sample(ps1_status_sequence=0), 0.3)
    assert not tracker.fresh(sample(ps1_status_sequence=0), 0.3)


def test_power_cycle_wrap_clears_old_confirmation_and_requires_progress():
    tracker = Ps1FeedbackTracker()
    tracker.observe(sample(ps1_power_cycle=65535), 0.0)
    tracker.observe(sample(ps1_power_cycle=65535, ps1_status_sequence=11), 0.1)
    assert tracker.fresh(sample(ps1_power_cycle=65535, ps1_status_sequence=11), 0.1)
    assert tracker.observe(sample(ps1_power_cycle=0, ps1_status_sequence=12), 0.2)
    assert not tracker.fresh(sample(ps1_power_cycle=0), 0.2)
    tracker.observe(sample(ps1_power_cycle=0, ps1_status_sequence=13), 0.3)
    assert tracker.fresh(sample(ps1_power_cycle=0, ps1_status_sequence=13), 0.3)


def test_reported_sample_age_limits_the_monotonic_deadline():
    tracker = Ps1FeedbackTracker()
    tracker.observe(sample(), 0.0)
    tracker.observe(sample(ps1_status_sequence=11, ps1_status_age_ms=4999), 0.1)
    assert tracker.fresh(sample(ps1_status_sequence=11), 0.1)
    assert not tracker.fresh(sample(ps1_status_sequence=11), 0.102)


def test_power_loss_invalidates_default_off_and_data_and_stops():
    _, client, controller = setup(running=True)
    client.set_status(main_ready=False, ps1_actual_output=False)
    controller.tick()
    status = controller.snapshot()["status"]
    assert status["PS1ActualOutput"] is None
    assert status["Voltage"] == [None] * 4
    assert status["PS1SetVoltage"] is None
    assert client.last_command.all_stop


def test_known_cpu_restart_requires_manual_restart_and_zero_on():
    _, client, controller = setup(running=True)
    before = len(client.command_history)
    controller.notify_cpu_restart()
    for _ in range(5):
        controller.tick()
    assert controller.phase == CabinetPhase.FAULT
    assert not any(c.ps1_output_enable for c in client.command_history[before:])


def test_f003_only_withdraws_run_permit_without_cutting_healthy_km0():
    _, client, controller = setup(running=True)
    client.set_status(ps1_mb_status=0xF003, setpoint_rejected=True,
        setpoint_applied=False, ps1_feedback_flags=0x4F)
    controller.tick()
    assert controller.phase == CabinetPhase.FAULT
    assert client.last_command.main_enable
    assert not client.last_command.ps1_output_enable
    assert not any(client.last_command.channel_enable)
    assert not client.last_command.all_stop


def test_heartbeat_cannot_resend_retained_on_after_feedback_expiry():
    clock, client, controller = setup(running=True)
    client.refresh_ps1_on_read = False
    clock.now += 5.0
    before = len(client.command_history)
    controller.status = replace(controller.status, received_monotonic=clock.now)
    controller.heartbeat()
    assert not any(c.ps1_output_enable for c in client.command_history[before:])


def test_cold_start_deadline_is_absolute_and_stop_cancels_it():
    clock, client, controller = setup()
    client.set_status(ps1_comm_ok=False)
    assert controller.request_start(user="offline", voltage=3.0, current=0.5)
    deadline = controller._startup_deadline
    for _ in range(10):
        controller.tick()
    assert controller.phase == CabinetPhase.WAIT_PS1_COMM
    assert controller._startup_deadline == deadline
    assert controller.timings.command_timeout_ms == 5000
    assert controller.request_all_stop(user="offline")
    clock.now = deadline + 1
    controller.tick()
    assert not controller.command.main_enable
    assert not controller._start_requested


def test_session_reset_discards_confirmation_even_when_counters_match():
    tracker = Ps1FeedbackTracker()
    tracker.observe(sample(), 0)
    tracker.observe(sample(ps1_status_sequence=11), 1)
    assert tracker.fresh(sample(ps1_status_sequence=11), 1)
    tracker.reset()
    tracker.observe(sample(ps1_status_sequence=11), 2)
    assert not tracker.fresh(sample(), 2)


def test_cold_start_total_deadline_cannot_be_renewed_by_repeated_snapshots():
    clock, client, controller = setup()
    assert controller.request_start(user="offline", voltage=3.0, current=0.5)
    deadline = controller._startup_deadline
    clock.now = deadline
    controller.tick()
    assert controller.phase == CabinetPhase.FAULT
    assert "总时间预算耗尽" in controller.last_error
    assert client.last_command.all_stop


@pytest.mark.parametrize("flags", [0xDF, 0xEF, 0x1CF])
def test_start_rejects_lock_exhaustion_reserved_bits_without_any_write(flags):
    _, client, controller = setup()
    client.set_status(ps1_feedback_flags=flags)
    controller.tick()
    before = len(client.command_history)
    assert not controller.request_start(user="offline", voltage=3.0, current=0.5)
    assert len(client.command_history) == before


def test_session_reconnect_never_replays_previously_running_requests():
    _, client, controller = setup(running=True)
    before = len(client.command_history)
    controller.disconnect()
    controller.connect(user="offline")
    for _ in range(5):
        controller.tick()
    assert controller.phase == CabinetPhase.OFF
    assert not any(c.main_enable or c.ps1_output_enable or any(c.channel_enable) for c in client.command_history[before:])


def test_default_off_and_port_stop_flags_without_real_status_do_not_admit():
    _, client, controller = setup(running=True)
    client.set_status(ps1_feedback_flags=0x0B, ps1_actual_output=False,
                      ps1_status_age_ms=65535)
    controller.tick()
    assert controller.snapshot()["status"]["PS1ActualOutput"] is None
    assert not controller.snapshot()["output_admission"]["allowed"]
    assert client.last_command.all_stop


def test_final_on_entry_f003_withdraws_run_requests_but_keeps_km0():
    _, client, controller = setup(running=True)
    controller.status = replace(controller.status, ps1_mb_status=0xF003,
        setpoint_rejected=True, setpoint_applied=False)
    before = len(client.command_history)
    controller._start_ps1_output("offline")
    assert not any(c.ps1_output_enable for c in client.command_history[before:])
    assert client.last_command.main_enable and not client.last_command.all_stop


def test_stale_tcp_frame_cannot_confirm_setpoint_or_issue_on():
    clock, client, controller = setup()
    assert controller.request_start(user="offline", voltage=3.0, current=0.5)
    for _ in range(30):
        controller.tick()
        if controller._pending and controller._pending.controls == ("Setpoints",):
            break
    client.refresh_on_read = False
    client.set_status(received_monotonic=clock.now - 2.0)
    before = len(client.command_history)
    controller.tick()
    assert controller.phase == CabinetPhase.STALE
    assert controller.feedback["Setpoints"].state.value != "confirmed"
    assert not any(c.ps1_output_enable for c in client.command_history[before:])


@pytest.mark.parametrize("version", [0x0100, 0x0101])
def test_legacy_snapshot_keeps_receipt_ability_but_not_confirmed_ps1_feedback(version):
    status = PlcStatus(protocol_version=version, analog_valid_mask=15,
                       ps1_actual_output=False, reset_result=ResetResult.SUCCEEDED)
    snapshot = status.to_dict()
    assert snapshot["ResetResultSupported"] == (version == 0x0101)
    assert not snapshot["ControlProtocolSupported"]
    assert snapshot["PS1ActualOutput"] is None
    assert not snapshot["PS1SetpointValid"] and not snapshot["PS1MeasurementValid"]
    assert snapshot["Voltage"] == [None] * 4
