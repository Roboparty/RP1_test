"""Qt fault aggregation and legacy-mode controls with all I/O disabled."""
from PySide6.QtCore import QTimer
from PySide6.QtWebSockets import QWebSocket
from PySide6.QtWidgets import QApplication
import pytest

from factory_hmi.desktop.app import FactoryMainWindow


def setup_without_io(window):
    window.reconnect_timer = QTimer(window)
    window.snapshot_timer = QTimer(window)
    window.lease_timer = QTimer(window)
    window.websocket = QWebSocket()


@pytest.fixture
def window(monkeypatch):
    application = QApplication.instance() or QApplication([])
    monkeypatch.setattr(FactoryMainWindow, "_setup_network", setup_without_io)
    monkeypatch.setattr(FactoryMainWindow, "_run_rest", lambda *args, **kwargs: None)
    result = FactoryMainWindow("http://127.0.0.1:65534")
    result.plc_page.stop()
    yield result
    result.plc_page.stop()
    result.deleteLater()
    assert application is not None


def receive(window, flags, *, version=0x0102, feedback=None):
    snapshot = {
        "communication_state": "LIVE", "stale": False, "driver": "mock",
        "phase": "FAULT" if flags else "OFF", "read_only": False,
        "status": {
            "ProtocolVersion": version, "ResetResultSupported": version in {0x0101, 0x0102},
            "FaultSourceFlags": flags, "FaultLatched": bool(flags & 1),
            "PS1Fault": bool(flags & 6), "PS1CommFault": bool(flags & 2),
            "PS1DeviceFault": bool(flags & 4), "FaultCode": 0,
            "PS1MbStatus": 0xF001, "PS1DeviceStatus": 0x12,
        },
        "feedback": feedback or {},
    }
    window.plc_page._receive(snapshot)


@pytest.mark.parametrize("flags,expected", [
    (1, ["PLC控制故障已锁存"]),
    (2, ["PLC↔PS1通信故障"]),
    (4, ["PS1设备故障"]),
    (7, ["PLC控制故障已锁存", "PLC↔PS1通信故障", "PS1设备故障"]),
])
def test_page_and_top_banner_show_each_fault_source(window, flags, expected):
    receive(window, flags)
    for text in expected:
        assert text in window.plc_page.fault.text()
        assert text in window.global_fault.text()
    assert "全局故障：无" not in window.global_fault.text()
    assert window.global_fault.objectName() == "faultBarError"
    if not flags & 1:
        assert "已锁存" not in window.plc_page.fault.text()


def test_independent_motor_and_cabinet_updates_cannot_clear_each_other(window):
    window._render_fault({"fault": {"message": "motor feedback stale"}})
    receive(window, 6)
    assert "motor feedback stale" in window.global_fault.text()
    assert "PLC↔PS1通信故障" in window.global_fault.text()
    receive(window, 0)
    assert "motor feedback stale" in window.global_fault.text()
    receive(window, 2)
    window._render_fault({"fault": None})
    assert "PLC↔PS1通信故障" in window.global_fault.text()
    assert "全局故障：无" not in window.global_fault.text()
    receive(window, 0)
    assert "全局故障：无" in window.global_fault.text()


def test_legacy_mode_disables_reset_and_explains_required_upgrade(window):
    receive(window, 1, version=0x0100)
    assert not window.plc_page.reset_button.isEnabled()
    assert "旧协议/无复位结果回执" in window.plc_page.reset_feedback.text()
    assert "V1.2" in window.plc_page.reset_button.toolTip()


def test_v11_retains_fault_display_but_cannot_control_new_flow(window):
    receive(window, 2, version=0x0101)
    assert "PLC↔PS1通信故障" in window.global_fault.text()
    assert not window.plc_page.start_button.isEnabled()
    assert not window.plc_page.reset_button.isEnabled()
    assert "V1.2" in window.plc_page.start_button.toolTip()


def test_unconfirmed_feedback_is_not_displayed_as_zero_or_off(window):
    receive(window, 0)
    assert "实际 未确认" in window.plc_page.ps1_state.text()
    assert "3  PS1输出  未确认" in window.plc_page.flow_labels[2].text()
    assert "TCP在线不代表设备反馈有效" in window.plc_page.setpoint_summary.text()
    assert "0.000" not in window.plc_page.setpoint_summary.text()


@pytest.mark.parametrize("state,detail", [
    ("pending", "等待PLC接收及复位结果"),
    ("rejected", "PLC拒绝复位：KM0反馈仍在"),
    ("failed", "PLC复位失败：控制故障仍锁存"),
])
def test_specific_reset_feedback_is_visible_on_page(window, state, detail):
    receive(window, 1, feedback={"ResetFaultPulse": {"state": state, "detail": detail}})
    assert detail in window.plc_page.reset_feedback.text()
    assert "PLC控制故障复位" in window.plc_page.reset_feedback.text()


def test_cabinet_voltage_chart_uses_confirmed_50v_transducer_limit(window):
    assert window.plc_page.voltage_chart.maximum == 50.0
    assert window.plc_page.current_chart.maximum == 50.0
