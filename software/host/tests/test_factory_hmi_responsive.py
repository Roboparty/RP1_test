from __future__ import annotations

import os
import unittest
from unittest import mock


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QTimer
from PySide6.QtWebSockets import QWebSocket
from PySide6.QtWidgets import QApplication, QComboBox, QDialog, QLineEdit, QTextEdit

from factory_hmi.desktop.app import FactoryMainWindow


def _setup_network_without_io(window: FactoryMainWindow) -> None:
    window.reconnect_timer = QTimer(window)
    window.snapshot_timer = QTimer(window)
    window.lease_timer = QTimer(window)
    window.websocket = QWebSocket()


class FactoryHmiResponsiveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        patcher = mock.patch.object(
            FactoryMainWindow,
            "_setup_network",
            _setup_network_without_io,
        )
        self.addCleanup(patcher.stop)
        patcher.start()
        with mock.patch.object(FactoryMainWindow, "_run_rest"):
            self.window = FactoryMainWindow("http://127.0.0.1:65534")

    def tearDown(self) -> None:
        self.window.deleteLater()

    def test_default_return_failure_replaces_the_moving_notice(self) -> None:
        from factory_hmi.client import GatewayHTTPError

        self.window.preflight_result.setText("正在低速回默认位")
        with mock.patch.object(self.window, "_run_rest"), mock.patch(
            "factory_hmi.desktop.app.QMessageBox.critical"
        ):
            self.window._rest_failed(
                "回默认位",
                GatewayHTTPError("failed", status_code=409, detail="return to default feedback error: id=3:error_id=7"),
                False,
            )
        self.assertIn("未完成", self.window.preflight_result.text())
        self.assertNotIn("正在", self.window.preflight_result.text())

    def test_logged_in_operator_can_start_local_test_without_upload_profile(self) -> None:
        self.window._platform_operator = "qa-operator"
        self.window.robot_id.setText("RP1.3-UPPER-01")
        self.window.platform_station_id.setText("A10")
        self.window.platform_case_code.setCurrentIndex(self.window.platform_case_code.findData("OTHER"))
        self.window.platform_test_profile.setCurrentIndex(0)
        with mock.patch("factory_hmi.desktop.app.QInputDialog.getText", return_value=("临时探索", True)), mock.patch.object(self.window, "_save_local_bench"), mock.patch.object(self.window, "_run_rest") as run, mock.patch.object(self.window.client, "playback_start") as start:
            self.window._start_playback()
            run.assert_called_once()
            run.call_args.args[1]()
            self.assertIsNone(start.call_args.kwargs["campaign_id"])
            self.assertEqual(start.call_args.kwargs["operator_id"], "qa-operator")
            self.assertEqual(start.call_args.kwargs["recording_scope"], "local")
            self.assertEqual(start.call_args.kwargs["bench_id"], "A10")
            self.assertEqual(start.call_args.kwargs["test_name"], "临时探索")

    def test_review_has_no_default_success_and_keeps_measured_data_readonly(self) -> None:
        self.window._platform_operator = "submit-account"
        self.window._platform_stations = [{"id": "11111111-1111-4111-8111-111111111111", "code": "A10", "active": True}]
        summary = {"timing": {"active_seconds": 1}, "quality": {}, "joint_metrics": [], "verdict": {"status": "blocked", "criteria_version": "UNCONFIGURED"}}
        def inspect(dialog):
            combos = dialog.findChildren(QComboBox)
            self.assertEqual(combos[0].itemData(1), "11111111-1111-4111-8111-111111111111")
            self.assertEqual(combos[1].currentText(), "请选择人工结论")
            self.assertEqual(dialog.findChildren(QLineEdit)[0].text(), "")
            editors = dialog.findChildren(QTextEdit)
            self.assertTrue(editors[0].isReadOnly())
            self.assertTrue(editors[1].isReadOnly())
            self.assertFalse(editors[2].isReadOnly())
            return QDialog.DialogCode.Rejected
        with mock.patch.object(QDialog, "exec", new=inspect), mock.patch.object(self.window, "_run_rest") as run:
            self.window._open_submission_review("synthetic", summary)
            run.assert_not_called()

    def test_summary_failure_cannot_open_empty_review(self) -> None:
        self.window.test_id.setProperty("record_id", "synthetic")
        with mock.patch.object(self.window, "_run_rest") as run:
            self.window._submit_execution()
            self.assertNotIn("on_failure", run.call_args.kwargs)

    def test_motor_fault_reset_failure_never_shows_success_or_waiting(self) -> None:
        from factory_hmi.client import GatewayHTTPError
        self.window.preflight_result.setText("正在复位电机故障并等待新反馈确认…")
        with mock.patch.object(self.window, "_run_rest"), mock.patch(
            "factory_hmi.desktop.app.QMessageBox.critical"
        ):
            self.window._rest_failed(
                "复位电机故障",
                GatewayHTTPError("failed", status_code=409, detail="after fault reset: fresh feedback timeout"),
                False,
            )
        self.assertIn("未确认", self.window.preflight_result.text())
        self.assertNotIn("正在", self.window.preflight_result.text())
        self.assertFalse(self.window._preflight_ok)

    def test_fault_reset_and_return_default_have_separate_state_gates(self) -> None:
        self.window._configured = True
        self.window._sync_state({
            "state": "fault", "connected": True,
            "station": {"motors_enabled": False}, "can": {"all_up": True},
        })
        self.assertTrue(self.window.motor_fault_reset_button.isEnabled())
        self.assertFalse(self.window.reset_button.isEnabled())
        self.window._sync_state({
            "state": "connected", "connected": True,
            "station": {"motors_enabled": True}, "can": {"all_up": True},
        })
        self.assertFalse(self.window.motor_fault_reset_button.isEnabled())
        self.assertTrue(self.window.reset_button.isEnabled())

    def test_selected_platform_profile_passes_its_bench_code_to_start(self) -> None:
        self.window.robot_id.setText("RP1.3-UPPER-01")
        self.window.platform_station_id.setText("MP-02")
        self.window.platform_case_code.addItem("REL-UPPER-001", "REL-UPPER-001")
        self.window.platform_case_code.setCurrentIndex(self.window.platform_case_code.count() - 1)
        self.window.platform_test_profile.addItem("Ready", {"ready": True, "bench_id": "MP-02", "test_case_id": "REL-UPPER-001", "sample_id": "RP1.3-UPPER-001"})
        self.window.platform_test_profile.setCurrentIndex(1)
        with mock.patch.object(self.window, "_save_local_bench"), mock.patch.object(self.window, "_run_rest") as run, mock.patch.object(self.window.client, "playback_start") as start:
            self.window._start_playback()
            run.call_args.args[1]()
            self.assertEqual(start.call_args.kwargs["recording_scope"], "platform")
            self.assertEqual(start.call_args.kwargs["bench_id"], "MP-02")
            self.assertEqual(start.call_args.kwargs["test_case_id"], "REL-UPPER-001")
            self.assertEqual(start.call_args.kwargs["robot_id"], "RP1.3-UPPER-001")

    def test_remote_code_is_displayed_but_actions_keep_local_record_id(self) -> None:
        local = "REL-SARM-001_2610101800_RP1.3-SARM-001_MP-01_" + "1" * 32
        official = "REL-SARM-001_2610101800_RP1.3-SARM-001_MP-01_E42"
        self.window.test_id.setProperty("record_id", local)
        self.window._execution_history_received({"items": [{
            "test_id": local, "execution_code": official,
            "execution_code_confirmed": True, "recording_scope": "platform",
            "sync_state": "uploaded",
        }]})
        self.assertEqual(self.window.execution_history_table.item(0, 0).text(), official)
        self.assertEqual(self.window.execution_history_table.item(0, 3).text(), "平台已接收")
        self.assertEqual(self.window.test_id.text(), official)
        self.window.execution_history_table.selectRow(0)
        self.assertEqual(self.window._selected_test_id(), local)
        with mock.patch.object(self.window, "_run_rest") as run, mock.patch.object(self.window.client, "retry_execution") as retry:
            self.window._retry_execution()
            run.call_args.args[1]()
            retry.assert_called_once_with(local)
        self.window.execution_history_table.clearSelection()
        self.window.execution_history_table.setCurrentCell(-1, -1)
        self.assertEqual(self.window._selected_test_id(), local)

    def test_incomplete_selected_profile_blocks_even_without_platform_login(self) -> None:
        self.window._platform_operator = ""
        self.window.robot_id.setText("local-arm")
        self.window.platform_test_profile.addItem("Incomplete", {"ready": False, "missing": ["campaign_id"]})
        self.window.platform_test_profile.setCurrentIndex(1)
        with mock.patch.object(self.window, "_run_rest") as run, mock.patch("factory_hmi.desktop.app.QMessageBox.warning") as warning:
            self.window._start_playback()
            run.assert_not_called()
            warning.assert_called_once()

    def test_incomplete_selected_upload_profile_still_blocks_start(self) -> None:
        self.window._platform_operator = "qa-operator"
        self.window.robot_id.setText("RP1.3-UPPER-01")
        self.window.platform_test_profile.addItem("Incomplete", {"ready": False, "missing": ["campaign_id"]})
        self.window.platform_test_profile.setCurrentIndex(1)
        with mock.patch.object(self.window, "_run_rest") as run, mock.patch("factory_hmi.desktop.app.QMessageBox.warning") as warning:
            self.window._start_playback()
            run.assert_not_called()
            warning.assert_called_once()

    def test_platform_login_is_in_the_fixed_header(self) -> None:
        header = self.window.platform_login_button.parent()
        self.assertEqual(header.objectName(), "appHeader")
        self.assertIs(self.window.platform_username.parent(), header)
        self.assertIs(self.window.platform_password.parent(), header)
        self.assertIs(self.window.platform_station_id.parent(), header)
        self.window.show()
        self.assertLess(
            self.window.platform_login_button.mapTo(self.window, self.window.platform_login_button.rect().topLeft()).y(),
            self.window.header_module_label.mapTo(self.window, self.window.header_module_label.rect().topLeft()).y(),
        )

    def test_presence_label_and_logout_follow_actual_gateway_state(self) -> None:
        self.window._render_platform_presence({"operator": "qa-operator", "presence": {"online": True}})
        self.assertIn("监控在线", self.window.platform_identity_label.text())
        self.assertTrue(self.window.platform_logout_button.isEnabled())
        self.window._render_platform_presence({"operator": "qa-operator", "presence": {"online": False}})
        self.assertIn("重新登录", self.window.platform_identity_label.text())
        with mock.patch.object(self.window, "_run_rest") as run:
            self.window._platform_logout()
            self.assertEqual(run.call_args.args[1], self.window.client.platform_logout)
            run.call_args.args[2]({"result": {"operator": ""}})
        self.assertEqual(self.window._platform_operator, "")
        self.assertFalse(self.window.platform_logout_button.isEnabled())

    def test_primary_fault_and_critical_bus_diagnosis_are_visible(self) -> None:
        self.window._render_fault({"fault": {
            "message": "feedback stale 2.0s",
            "diagnoses": [
                {"severity": "warning", "suspected_component": "motor:2", "evidence": ["tracking error"]},
                {"severity": "critical", "suspected_component": "communication_bus:can0", "evidence": ["7 motors stale"]},
            ],
        }})
        text = self.window.global_fault.text()
        self.assertIn("feedback stale 2.0s", text)
        self.assertIn("communication_bus:can0", text)
        self.assertIn("7 motors stale", text)
        self.assertNotIn("疑似部件：motor:2", text)

    def test_fault_message_survives_missing_or_invalid_diagnoses(self) -> None:
        for diagnoses in (None, [], [None, "invalid"]):
            with self.subTest(diagnoses=diagnoses):
                self.window._render_fault({"fault": {"message": "driver failed", "diagnoses": diagnoses}})
                self.assertIn("driver failed", self.window.global_fault.text())
                self.assertEqual(self.window.global_fault.objectName(), "faultBarError")

    def test_fault_banner_clears_after_fault_is_removed(self) -> None:
        self.window._render_fault({"fault": "fault"})
        self.window._render_fault({"fault": None})
        self.assertIn("全局故障：无", self.window.global_fault.text())
        self.assertEqual(self.window.global_fault.objectName(), "faultBarOk")

    def test_supported_resolutions_exceed_required_shell_minimums(self) -> None:
        minimum = self.window.minimumSize()
        for width, height in ((1600, 900), (1600, 1000), (1920, 1080)):
            with self.subTest(size=(width, height)):
                self.assertLessEqual(minimum.width(), width)
                self.assertLessEqual(minimum.height(), height)
        self.assertEqual(self.window._nav_buttons[0].parent().width(), 216)
        self.assertEqual(self.window.centralWidget().layout().contentsMargins().top(), 0)
        self.assertEqual(self.window.tabs.minimumWidth(), 0)
        self.assertEqual(self.window.tabs.minimumHeight(), 0)

    def test_all_critical_controls_remain_in_their_page_hierarchy(self) -> None:
        controls = (
            (self.window.disconnect_button, 0),
            (self.window.disable_button, 1),
            (self.window.save_csv_button, 1),
            (self.window.next_run_button, 1),
            (self.window.manual_stop_button, 2),
            (self.window.manual_disable_button, 2),
        )
        for control, page_index in controls:
            with self.subTest(control=control.text()):
                ancestor = control.parentWidget()
                stack_page = self.window.tabs.widget(page_index)
                while ancestor is not None and ancestor is not stack_page:
                    ancestor = ancestor.parentWidget()
                self.assertIs(ancestor, stack_page)


if __name__ == "__main__":
    unittest.main()
