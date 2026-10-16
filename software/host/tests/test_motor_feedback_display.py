"""Feedback display regressions using Qt with network and device I/O disabled."""
import math

import pytest
from PySide6.QtCore import QTimer, Qt
from PySide6.QtWebSockets import QWebSocket
from PySide6.QtWidgets import QApplication

from factory_hmi.desktop.app import FactoryMainWindow


@pytest.fixture
def window(monkeypatch):
    application = QApplication.instance() or QApplication([])

    def setup_without_io(instance):
        instance.reconnect_timer = QTimer(instance)
        instance.snapshot_timer = QTimer(instance)
        instance.lease_timer = QTimer(instance)
        instance.websocket = QWebSocket()

    monkeypatch.setattr(FactoryMainWindow, "_setup_network", setup_without_io)
    monkeypatch.setattr(FactoryMainWindow, "_run_rest", lambda *args, **kwargs: None)
    result = FactoryMainWindow("http://127.0.0.1:65534")
    result.plc_page.stop()
    yield result
    result.plc_page.stop()
    result.deleteLater()
    assert application is not None


def receive(window, **overrides):
    motor = {
        "joint": "left_shoulder_pitch_joint", "motor_id": 1, "can": "can0",
        "timestamp_s": 123.0, "feedback_age_s": 0.01,
        "position_rad": 0.1, "speed_rad_s": 0.2, "torque_nm": 0.3,
        "temperature_c": 25.0, "bus_voltage_v": 48.0, "bus_current_a": 0.5,
        "error_id": 0,
    }
    motor.update(overrides)
    window._render_snapshot({
        "state": "connected", "connected": True,
        "station": {"state": "connected", "motors_discovered": True,
                    "motors_enabled": False},
        "motors": [motor],
    })


def cell(window, column):
    return window.motor_table_model.index(0, column).data(Qt.DisplayRole)


@pytest.mark.parametrize("timestamp", [0.0, math.nan])
def test_no_feedback_is_not_normal_or_uptime_since_boot(window, timestamp):
    receive(window, timestamp_s=timestamp, feedback_age_s=952699.344)
    assert cell(window, 12) == "未收到电机反馈"
    assert cell(window, 13) == "—"
    assert cell(window, 11) == "—"
    for column in range(4, 11):
        assert cell(window, column) == "—"


def test_fresh_feedback_retains_readings_and_normal_status(window):
    receive(window)
    assert cell(window, 12) == "正常"
    assert cell(window, 4) != "—"
    assert cell(window, 11) == "0"
    assert cell(window, 13) != "—"


def test_lost_feedback_reports_timeout_and_retains_observed_motor_error(window):
    receive(window, feedback_age_s=0.3, error_id=7)
    assert cell(window, 12) == "通信反馈超时"
    assert cell(window, 4) == "—"
    assert cell(window, 11) == "7"
    assert cell(window, 13) != "—"


def test_nonzero_error_is_not_hidden_by_missing_timestamp(window):
    receive(window, timestamp_s=0.0, error_id=7)
    assert cell(window, 11) == "7"
    assert cell(window, 12) == "未收到电机反馈"


def test_existing_software_alarm_is_not_replaced(window):
    receive(window, timestamp_s=0.0, software_diagnosis="位置跟踪超限")
    assert cell(window, 12) == "位置跟踪超限"
    assert cell(window, 13) == "—"
