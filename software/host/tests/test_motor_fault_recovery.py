"""Offline recovery ordering and fresh-feedback gates; no hardware transports."""
from __future__ import annotations

import concurrent.futures
import threading
import time
from dataclasses import replace
from unittest import mock

import numpy as np
import pytest
from fastapi.testclient import TestClient

from factory_hmi import FactoryController, SafetyLimits, StationConfig, StationState
from factory_hmi.core.backend import FakeMotorBackend, MotorSafetyError, MotorsPyBackend
from factory_hmi.gateway.app import create_app


class ObservedBackend(FakeMotorBackend):
    def __init__(self):
        super().__init__(2)
        self.events = []
        self.clear_effective = True
        self.freeze_after_clear = False
        self.freeze_after_enable = False
        self.partial_enable = False
        self.cleared = threading.Event()

    def _open_transport_impl(self):
        self.events.append("open")
        super()._open_transport_impl()

    def _refresh_feedback_impl(self):
        self.events.append("feedback")
        super()._refresh_feedback_impl()

    def _clear_errors_impl(self, index):
        assert not self.motors_enabled
        self.events.append("clear")
        if self.clear_effective:
            super()._clear_errors_impl(index)
        if self.freeze_after_clear:
            self.stale[:] = True
        self.cleared.set()

    def _enable_motors_impl(self):
        self.events.append("enable")
        if self.partial_enable:
            self.enabled[0] = True
            raise MotorSafetyError("partial enable failure")
        super()._enable_motors_impl()
        if self.freeze_after_enable:
            self.stale[:] = True


@pytest.fixture
def station():
    backend = ObservedBackend()
    config = StationConfig(
        limb="left_arm", entries=backend.entries,
        raw={"factory_hmi_faults": {"stale_seconds": 0.08}},
        control_rate_hz=100.0, safety_limits=SafetyLimits(),
    )
    controller = FactoryController(config, backend, enable_watchdog=False)
    with mock.patch.object(controller, "_start_position_hold") as hold:
        yield controller, backend, hold
    controller.disconnect()


def test_fault_reset_confirms_replies_then_leaves_disabled(station):
    controller, backend, hold = station
    backend.inject_error(0, 7)
    controller.discover_motors()
    with pytest.raises(RuntimeError, match="before motor enable.*error_id=7"):
        controller.enable_motors()
    assert "enable" not in backend.events
    assert controller.state is StationState.FAULT

    controller.reset_motor_fault()
    assert controller.state is StationState.CONNECTED
    assert backend.connected and not backend.motors_enabled
    assert not np.any(backend.enabled)
    assert backend.events[backend.events.index("clear") - 1] == "feedback"
    assert backend.events.count("clear") == 1
    assert "enable" not in backend.events
    hold.assert_not_called()

    controller.enable_motors()
    assert backend.events.index("clear") < backend.events.index("enable")
    hold.assert_called_once()


def test_cached_zero_error_without_new_frames_cannot_pass_clear(station):
    controller, backend, hold = station
    backend.freeze_after_clear = True
    with pytest.raises(RuntimeError, match="after fault reset: fresh feedback timeout"):
        controller.reset_motor_fault()
    assert not np.any(backend.error_ids)  # zero cache alone is insufficient
    assert controller.state is StationState.FAULT
    assert not backend.motors_enabled
    assert backend.events.count("clear") == 1  # never automatically resent
    hold.assert_not_called()


def test_persistent_fresh_driver_fault_does_not_claim_reset_success(station):
    controller, backend, hold = station
    backend.inject_error(1, 7)
    backend.clear_effective = False
    with pytest.raises(RuntimeError, match="after fault reset.*id=2:error_id=7"):
        controller.reset_motor_fault()
    assert controller.state is StationState.FAULT
    assert "enable" not in backend.events
    hold.assert_not_called()


def test_discovery_rejects_missing_motor_without_sending_enable(station):
    controller, backend, hold = station
    backend.set_stale(1)
    with pytest.raises(RuntimeError, match="motor discovery: fresh feedback timeout: motor_ids=2"):
        controller.discover_motors()
    assert "clear" not in backend.events and "enable" not in backend.events
    assert controller.state is StationState.FAULT
    hold.assert_not_called()


def test_missing_feedback_before_reset_does_not_send_clear(station):
    controller, backend, hold = station
    backend.set_stale(0)
    with pytest.raises(RuntimeError, match="before fault reset: fresh feedback timeout"):
        controller.reset_motor_fault()
    assert "clear" not in backend.events
    hold.assert_not_called()


@pytest.mark.parametrize("failure", ["freeze_after_enable", "partial_enable"])
def test_enable_failure_disables_group_and_never_starts_hold(station, failure):
    controller, backend, hold = station
    controller.discover_motors()
    setattr(backend, failure, True)
    with pytest.raises(RuntimeError):
        controller.enable_motors()
    assert controller.state is StationState.FAULT
    assert not backend.motors_enabled and not np.any(backend.enabled)
    hold.assert_not_called()


def test_position_limit_checked_before_enable_command(station):
    controller, backend, hold = station
    controller.config = replace(controller.config, safety_limits=SafetyLimits(motor_max_rad=0.5))
    backend.positions[0] = 0.8
    controller.discover_motors()
    with pytest.raises(RuntimeError, match="violates motor_max_rad"):
        controller.enable_motors()
    assert "enable" not in backend.events
    hold.assert_not_called()


def test_return_default_cannot_reset_or_enable_disabled_station(station):
    controller, backend, hold = station
    controller.discover_motors()
    with pytest.raises(RuntimeError, match="enable healthy motors"):
        controller.reset(to_default=True)
    assert "clear" not in backend.events and "enable" not in backend.events
    hold.assert_not_called()


def test_fault_reset_requires_explicit_disable_first(station):
    controller, backend, hold = station
    controller.connect()
    with mock.patch.object(controller, "_stop_position_hold") as stop_hold:
        with pytest.raises(RuntimeError, match="disable motors before resetting"):
            controller.reset_motor_fault()
        stop_hold.assert_not_called()
    assert "clear" not in backend.events
    assert backend.motors_enabled


def test_stop_interrupts_confirmation_wait_and_preserves_fault(station):
    controller, backend, hold = station
    backend.freeze_after_clear = True
    with concurrent.futures.ThreadPoolExecutor() as pool:
        resetting = pool.submit(controller.reset_motor_fault)
        assert backend.cleared.wait(1.0)
        assert controller.request_motion_stop()
        with pytest.raises(RuntimeError, match="interrupted by stop request"):
            resetting.result(timeout=1.0)
    assert controller.state is StationState.FAULT
    assert not backend.motors_enabled
    hold.assert_not_called()


def test_recovery_endpoint_authentication_and_disabled_result(station, tmp_path, monkeypatch):
    controller, backend, hold = station
    monkeypatch.setenv("RP1_FACTORY_OPERATOR_TOKEN", "offline-only-test-token")
    with TestClient(create_app(controller, data_root=tmp_path, lease_timeout_s=30.0)) as client:
        denied = client.post("/api/v1/motors/reset-fault", headers={"X-RP1-Client-ID": "operator-a"})
        assert denied.status_code == 401
        assert not backend.events
        headers = {"X-RP1-Client-ID": "operator-a", "X-RP1-Operator-Token": "offline-only-test-token"}
        client.app.state.runtime.session("operator-a").lease.claim("operator-a")
        foreign = dict(headers, **{"X-RP1-Client-ID": "operator-b"})
        assert client.post("/api/v1/motors/reset-fault", headers=foreign).status_code == 409
        assert not backend.events
        reset = client.post("/api/v1/motors/reset-fault", headers=headers)
        assert reset.status_code == 200, reset.text
        assert reset.json()["snapshot"]["station"]["motors_enabled"] is False
        assert not backend.command_history
        hold.assert_not_called()


def test_gateway_disable_interrupts_fault_reset_without_enabling(station, tmp_path):
    controller, backend, hold = station
    controller._diagnosis.thresholds = replace(controller._diagnosis.thresholds, stale_seconds=1.0)
    backend.freeze_after_clear = True
    with TestClient(create_app(controller, data_root=tmp_path, lease_timeout_s=30.0)) as client:
        headers = {"X-RP1-Client-ID": "operator-a"}
        with concurrent.futures.ThreadPoolExecutor() as pool:
            resetting = pool.submit(client.post, "/api/v1/motors/reset-fault", headers=headers)
            assert backend.cleared.wait(1.0)
            stopped = client.post("/api/v1/motors/disable", headers=headers)
            assert stopped.status_code == 200, stopped.text
            reset = resetting.result(timeout=1.0)
            assert reset.status_code == 409
            assert "interrupted" in reset.json()["detail"]
    assert "enable" not in backend.events
    assert not backend.command_history
    hold.assert_not_called()


def test_sequence_capable_sdk_cannot_use_zero_response_counter_as_new_reply():
    class SilentMotor:
        def refresh_motor_status(self):
            pass

        def get_feedback_count(self):
            return 0

        def get_response_count(self):
            return 0

    template = FakeMotorBackend(1).entries
    backend = MotorsPyBackend([replace(template[0], motor_type="LRO")])
    backend._connected = True
    backend._motors = [SilentMotor()]
    backend._last_feedback_counts = [0]
    try:
        backend.refresh_feedback()
        assert backend._last_feedback_at[0] == 0.0
    finally:
        backend._connected = False


def test_position_hold_rejects_stale_feedback_before_sending_target(station):
    controller, backend, hold = station
    controller.connect()
    backend.set_stale([0, 1])
    backend.feedback_timestamps[:] = time.monotonic() - 1.0
    controller._position_hold_stop = threading.Event()
    controller._position_hold_loop(np.zeros(2), controller._position_hold_stop)
    assert controller.state is StationState.FAULT
    assert not backend.motors_enabled
    assert not backend.command_history


def test_sdk_without_receive_counters_cannot_refresh_cached_sample_age():
    getters = ["get_motor_pos", "get_motor_spd", "get_motor_current", "get_motor_temperature", "get_error_id"]
    motor = mock.Mock(spec=getters)
    for name in getters:
        getattr(motor, name).return_value = 0
    backend = MotorsPyBackend(FakeMotorBackend(1).entries)
    backend._connected = True
    backend._motors = [motor]
    try:
        first = backend.read_samples()[0]
        backend.refresh_feedback()
        second = backend.read_samples()[0]
        assert first.timestamp_s == second.timestamp_s == 0.0
        assert second.feedback_age_s > 2.0
    finally:
        backend._connected = False
