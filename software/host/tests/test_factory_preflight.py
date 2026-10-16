"""Offline regressions; all HMI network work and real motor construction denied."""
from __future__ import annotations

import os
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest
pytest.importorskip("PySide6", reason="desktop extra required for offline Qt regressions")

from PySide6.QtCore import QTimer
from PySide6.QtWebSockets import QWebSocket
from PySide6.QtWidgets import QApplication

from factory_hmi import FakeMotorBackend, FactoryController, SafetyLimits, StationConfig, StationState
from factory_hmi.client import GatewayClient, GatewayHTTPError
from factory_hmi.core.backend import MotorsPyBackend
from factory_hmi.core.trajectory import TrajectoryValidationError
from factory_hmi.desktop.app import FactoryMainWindow
from factory_hmi.desktop.plc_page import PlcCabinetPage


def deny_io(*args, **kwargs):
    raise AssertionError("Offline test attempted network/physical backend I/O")


@pytest.fixture(autouse=True)
def network_and_hardware_denied(monkeypatch):
    monkeypatch.setattr(GatewayClient, "_request", deny_io)
    monkeypatch.setattr(GatewayClient, "snapshot", deny_io)
    monkeypatch.setattr(MotorsPyBackend, "__init__", deny_io)


@pytest.fixture
def window(monkeypatch):
    application = QApplication.instance() or QApplication([])

    def setup_without_io(w):
        w.reconnect_timer = QTimer(w)
        w.snapshot_timer = QTimer(w)
        w.lease_timer = QTimer(w)
        w.websocket = QWebSocket()

    monkeypatch.setattr(FactoryMainWindow, "_setup_network", setup_without_io)
    monkeypatch.setattr(FactoryMainWindow, "_run_rest", lambda *a, **kw: None)
    monkeypatch.setattr(PlcCabinetPage, "refresh", lambda *a, **kw: None)
    w = FactoryMainWindow("http://127.0.0.1:65534")
    w.plc_page.stop()
    w._configured = True
    w._trajectory_imported = True
    w._last_trajectory = trajectory_summary()
    yield w
    w.plc_page.stop()
    w.deleteLater()
    application.processEvents()


def trajectory_summary():
    return {
        "sha256": "f" * 64, "frames": 8, "duration_s": 0.035,
        "preflight": {"safe": True, "warnings": []},
        "preflight_violations": [],
    }


def snapshot(state="connected", *, enabled=True, trajectory=True, top_state=None):
    return {
        "configured": True, "state": top_state or state, "connected": True,
        "station": {
            "state": state, "backend_connected": True, "motors_enabled": enabled,
            "position_hold_active": enabled, "limb": "left_arm",
        },
        "can": {"all_up": True},
        "trajectory": trajectory_summary() if trajectory else None,
    }


def test_connected_with_trajectory_and_enabled_motors_can_preflight(window):
    window._render_snapshot(snapshot())
    assert window.preflight_button.isEnabled()
    assert not window.start_button.isEnabled()


def test_station_state_is_authoritative_over_top_level_stale_state(window):
    window._render_snapshot(snapshot("connected", top_state="armed"))
    assert window.preflight_button.isEnabled()
    assert not window.start_button.isEnabled()
    assert not window._preflight_ok


def test_snapshot_trajectory_restores_a_missing_local_import_marker(window):
    window._trajectory_imported = False
    window._last_trajectory = {}
    window._render_snapshot(snapshot())
    assert window._trajectory_imported
    assert window.preflight_button.isEnabled()


def test_snapshot_without_trajectory_disables_stale_local_import_marker(window):
    window._render_snapshot(snapshot(trajectory=False))
    assert not window.preflight_button.isEnabled()
    assert not window.start_button.isEnabled()


@pytest.mark.parametrize("state", ["disconnected", "armed", "running", "paused", "completed", "fault"])
def test_non_connected_states_cannot_preflight(window, state):
    window._render_snapshot(snapshot(state))
    assert not window.preflight_button.isEnabled()


def test_disabled_motors_cannot_preflight(window):
    window._render_snapshot(snapshot(enabled=False))
    assert not window.preflight_button.isEnabled()
    assert not window.start_button.isEnabled()


@pytest.mark.parametrize("state,enabled,trajectory", [
    ("fault", True, True),
    ("running", True, True),
    ("completed", True, True),
    ("connected", False, True),
    ("connected", True, False),
])
def test_disabled_preflight_explains_why(window, state, enabled, trajectory):
    window._render_snapshot(snapshot(state, enabled=enabled, trajectory=trajectory))
    assert not window.preflight_button.isEnabled()
    assert window.preflight_button.toolTip().strip()


@pytest.mark.parametrize("control", ["preflight_button", "start_button"])
def test_disabled_named_buttons_do_not_look_active(window, control):
    window._render_snapshot(snapshot("fault"))
    button = getattr(window, control)
    button.ensurePolished()
    assert not button.isEnabled()
    assert button.palette().button().color().name() not in {"#2375c9", "#1f9d69"}


def test_default_return_correct_connected_snapshot_restores_preflight(window):
    result = snapshot()
    window._default_returned({"ok": True, "result": result, "snapshot": result}, recheck=False)
    assert window.preflight_button.isEnabled()
    assert not window.start_button.isEnabled()
    assert "请执行安全预检" in window.preflight_result.text()


@pytest.mark.parametrize("returned_state", ["connected", "armed"])
def test_snapshot_cannot_reenable_preflight_or_start_during_default_return(window, returned_state):
    window._render_snapshot(snapshot())
    with mock.patch.object(window, "_run_rest") as request:
        window._start_default_return(recheck=False)
        assert request.call_count == 1
        window._render_snapshot(snapshot(returned_state))
        assert not window.preflight_button.isEnabled()
        assert not window.start_button.isEnabled()


@pytest.mark.parametrize("returned_state", ["connected", "armed"])
def test_snapshot_cannot_reenable_preflight_or_start_during_arm_request(window, returned_state):
    window._render_snapshot(snapshot())
    with mock.patch.object(window, "_run_rest") as request:
        window._confirm_preflight()
        assert request.call_count == 1
        window._render_snapshot(snapshot(returned_state))
        assert not window.preflight_button.isEnabled()
        assert not window.start_button.isEnabled()


def test_duplicate_arm_requests_are_not_scheduled(window):
    window._render_snapshot(snapshot())
    with mock.patch.object(window, "_run_rest") as request:
        window._confirm_preflight()
        window._confirm_preflight()
        assert request.call_count == 1


def test_duplicate_default_return_requests_are_not_scheduled(window):
    window._render_snapshot(snapshot())
    with mock.patch.object(window, "_run_rest") as request:
        window._start_default_return(recheck=False)
        window._start_default_return(recheck=False)
        assert request.call_count == 1


def test_real_qt_preflight_button_click_schedules_one_arm(window):
    window._render_snapshot(snapshot())
    with mock.patch.object(window, "_run_rest") as request:
        window.preflight_button.click()
        window.preflight_button.click()
        assert request.call_count == 1
        assert request.call_args.args[1] == window.client.arm
        assert not window.preflight_button.isEnabled()


def test_arm_success_callback_and_finish_release_pending_then_enable_start(window):
    window._render_snapshot(snapshot())
    with mock.patch.object(window, "_run_rest") as request:
        window._confirm_preflight()
        call = request.call_args
        armed = snapshot("armed")
        call.args[2]({"ok": True, "snapshot": armed, "result": armed})
        call.kwargs["on_finished"]()
        assert not window._preflight_pending
        assert window._preflight_ok
        assert window.start_button.isEnabled()
        assert not window.preflight_button.isEnabled()


def test_arm_failure_callback_and_finish_release_pending_allow_retry(window):
    window._render_snapshot(snapshot())
    with mock.patch.object(window, "_run_rest") as request, mock.patch("factory_hmi.desktop.app.QMessageBox.critical"):
        window._confirm_preflight()
        call = request.call_args
        window._rest_failed("进入 Armed", GatewayHTTPError("failed", status_code=409, detail="offline test refusal"), False)
        call.kwargs["on_finished"]()
        assert not window._preflight_pending
        assert not window._preflight_ok
        assert window.preflight_button.isEnabled()
        assert not window.start_button.isEnabled()


def test_first_frame_refusal_releases_pending_before_recovery_handler(window):
    window._render_snapshot(snapshot())
    with mock.patch.object(window, "_run_rest"), mock.patch.object(window, "_handle_first_frame_preflight_failure") as recovery:
        window._confirm_preflight()
        observed = []
        recovery.side_effect = lambda detail: observed.append(window._preflight_pending)
        window._rest_failed("进入 Armed", GatewayHTTPError("failed", status_code=409, detail="motor[0] first-frame delta too large: 1 > 0.2 rad"), False)
        assert observed == [False]


def test_default_return_success_callback_and_finish_release_pending(window):
    window._render_snapshot(snapshot())
    with mock.patch.object(window, "_run_rest") as request:
        window._start_default_return(recheck=False)
        call = request.call_args
        returned = snapshot()
        call.args[2]({"ok": True, "result": returned, "snapshot": returned})
        call.kwargs["on_finished"]()
        assert not window._default_return_pending
        assert window.preflight_button.isEnabled()
        assert not window.start_button.isEnabled()


def test_default_return_failure_callback_and_finish_release_pending(window):
    window._render_snapshot(snapshot())
    with mock.patch.object(window, "_run_rest") as request, mock.patch("factory_hmi.desktop.app.QMessageBox.critical"):
        window._start_default_return(recheck=False)
        call = request.call_args
        window._rest_failed("回默认位", GatewayHTTPError("failed", status_code=409, detail="offline test refusal"), False)
        call.kwargs["on_finished"]()
        assert not window._default_return_pending
        assert not window._preflight_ok
        assert window.reset_button.isEnabled()
        assert not window.start_button.isEnabled()


def test_trajectory_upload_response_applies_snapshot_without_waiting_for_poll(window):
    window._motors_enabled = False
    window._trajectory_imported = False
    window._last_snapshot = snapshot("completed", enabled=False, trajectory=False)
    received = snapshot()
    window._trajectory_imported_ok({"ok": True, "result": trajectory_summary(), "snapshot": received})
    assert window._last_snapshot == received
    assert window._motors_enabled
    assert window._trajectory_imported
    assert window.preflight_button.isEnabled()
    assert not window.start_button.isEnabled()


def test_first_frame_recovery_waits_for_return_then_runs_actual_arm_again(window):
    window._render_snapshot(snapshot())
    with mock.patch.object(window, "_run_rest") as request, mock.patch.object(
        window, "_handle_first_frame_preflight_failure",
        side_effect=lambda detail: window._start_default_return(recheck=True),
    ):
        window._confirm_preflight()
        first_arm = request.call_args
        window._rest_failed(
            "进入 Armed",
            GatewayHTTPError("failed", status_code=409, detail="motor[0] first-frame delta too large: 1 > 0.2 rad"),
            False,
        )
        returning = next(c for c in request.call_args_list if c.args[0] == "回默认位")
        # Completion of the rejected arm cannot release the active return gate.
        first_arm.kwargs["on_finished"]()
        assert window._default_return_pending
        assert not window.preflight_button.isEnabled()
        assert not window.start_button.isEnabled()

        returned = snapshot()
        returning.args[2]({"ok": True, "result": returned, "snapshot": returned})
        returning.kwargs["on_finished"]()
        QApplication.instance().processEvents()
        arms = [c for c in request.call_args_list if c.args[0] == "进入 Armed"]
        assert len(arms) == 2
        second_arm = arms[-1]
        assert second_arm.args[1] == window.client.arm
        assert window._preflight_pending
        assert not window.start_button.isEnabled()

        armed = snapshot("armed")
        second_arm.args[2]({"ok": True, "snapshot": armed, "result": armed})
        second_arm.kwargs["on_finished"]()
        assert not window._preflight_pending
        assert not window._default_return_pending
        assert window._preflight_ok
        assert window.start_button.isEnabled()


def test_unsafe_import_report_still_blocks_ui_arm(window):
    window._render_snapshot(snapshot())
    window._last_trajectory = {"preflight": {"safe": False}, "preflight_violations": ["limit exceeded"]}
    with mock.patch.object(window, "_run_rest") as request, mock.patch("factory_hmi.desktop.app.QMessageBox.critical"):
        window._confirm_preflight()
        request.assert_not_called()
    assert not window._preflight_ok


@pytest.fixture
def fake_controller():
    backend = FakeMotorBackend(2)
    config = StationConfig(
        limb="left_arm", entries=backend.entries, raw={}, control_rate_hz=20,
        safety_limits=SafetyLimits(max_first_frame_delta_rad=0.2),
    )
    c = FactoryController(config, backend, enable_watchdog=False)
    with mock.patch.object(c, "_start_position_hold"):
        c.connect()
        yield c, backend
        c.disconnect()


def write_trajectory(directory: Path, name="trajectory.npz", position=0.0):
    path = directory / name
    timestamps = np.array([0.0, 0.02, 0.04])
    np.savez(path, motor_pos=np.full((3, 2), position), fps=np.array([50.0]), time=timestamps, limb=np.array(["left_arm"]))
    return path


def complete_without_control_threads(c):
    for state in [StationState.RUNNING, StationState.STOPPING, StationState.COMPLETED]:
        c.state_machine.transition(state)


def test_completed_successful_new_import_returns_connected_and_requires_new_arm(fake_controller, tmp_path):
    c, backend = fake_controller
    c.load_trajectory(write_trajectory(tmp_path, "old.npz"))
    c.arm()
    complete_without_control_threads(c)
    assert c.state is StationState.COMPLETED
    new = c.load_trajectory(write_trajectory(tmp_path, "new.npz"))
    assert c.state is StationState.CONNECTED
    assert new.sha256 == c.snapshot()["trajectory"]["sha256"]
    assert backend.motors_enabled
    c.arm()
    assert c.state is StationState.ARMED


def test_armed_new_import_is_still_rejected(fake_controller, tmp_path):
    c, backend = fake_controller
    c.load_trajectory(write_trajectory(tmp_path, "old.npz"))
    c.arm()
    with pytest.raises(RuntimeError):
        c.load_trajectory(write_trajectory(tmp_path, "new.npz"))
    assert c.state is StationState.ARMED


def test_failed_new_import_does_not_change_completed_state(fake_controller, tmp_path):
    c, backend = fake_controller
    old = c.load_trajectory(write_trajectory(tmp_path))
    c.arm()
    complete_without_control_threads(c)
    corrupt = tmp_path / "broken.npz"
    corrupt.write_bytes(b"not an npz")
    with pytest.raises(Exception):
        c.load_trajectory(corrupt)
    assert c.state is StationState.COMPLETED
    assert c.snapshot()["trajectory"]["sha256"] == old.sha256


def test_new_import_cannot_bypass_live_first_frame_check(fake_controller, tmp_path):
    c, backend = fake_controller
    c.load_trajectory(write_trajectory(tmp_path))
    c.arm()
    complete_without_control_threads(c)
    c.load_trajectory(write_trajectory(tmp_path, "next.npz"))
    backend.positions[:] = [1.0, 0.0]
    with pytest.raises((RuntimeError, TrajectoryValidationError), match="first-frame delta too large"):
        c.arm()
    assert c.state is not StationState.ARMED


def test_new_import_cannot_bypass_live_motor_error_check(fake_controller, tmp_path):
    c, backend = fake_controller
    c.load_trajectory(write_trajectory(tmp_path))
    c.arm()
    complete_without_control_threads(c)
    c.load_trajectory(write_trajectory(tmp_path, "next.npz"))
    backend.inject_error(1, 7)
    with pytest.raises(RuntimeError, match="cannot arm while motors report errors"):
        c.arm()
    assert c.state is StationState.FAULT


def test_connected_arm_live_first_frame_limit_already_rejects(fake_controller, tmp_path):
    c, backend = fake_controller
    c.load_trajectory(write_trajectory(tmp_path))
    backend.positions[:] = [1.0, 0.0]
    with pytest.raises(TrajectoryValidationError, match="first-frame delta too large"):
        c.arm()
    assert c.state is StationState.CONNECTED


def test_connected_arm_live_motor_error_already_rejects(fake_controller, tmp_path):
    c, backend = fake_controller
    c.load_trajectory(write_trajectory(tmp_path))
    backend.inject_error(1, 7)
    with pytest.raises(RuntimeError, match="cannot arm while motors report errors"):
        c.arm()
    assert c.state is StationState.FAULT


def test_backend_fault_state_arm_is_rejected_without_commands(fake_controller, tmp_path):
    c, backend = fake_controller
    c.load_trajectory(write_trajectory(tmp_path))
    c.state_machine.transition(StationState.FAULT)
    before = len(backend.command_history)
    with pytest.raises(RuntimeError, match="requires state.*connected"):
        c.arm()
    assert c.state is StationState.FAULT
    assert len(backend.command_history) == before
