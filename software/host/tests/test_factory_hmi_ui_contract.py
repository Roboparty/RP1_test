"""Current desktop controls exercised with Qt and an isolated HTTP transport."""
from __future__ import annotations

import json
import os
import time
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEvent, QTimer
from PySide6.QtWebSockets import QWebSocket
from PySide6.QtWidgets import QApplication, QMessageBox

from factory_hmi.client import GatewayClient, GatewayError
from factory_hmi.desktop.app import FactoryMainWindow
from factory_hmi.desktop.plc_page import PlcCabinetPage


def _setup_network_without_io(window: FactoryMainWindow) -> None:
    window.reconnect_timer = QTimer(window)
    window.snapshot_timer = QTimer(window)
    window.lease_timer = QTimer(window)
    window.websocket = QWebSocket()


class FactoryHmiUiContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        # Keep even deferred refreshes and accidental requests offline.
        for patcher in (
            mock.patch.object(FactoryMainWindow, "_setup_network", _setup_network_without_io),
            mock.patch.object(PlcCabinetPage, "refresh"),
            mock.patch("factory_hmi.client.request.urlopen", side_effect=AssertionError("unexpected HTTP request")),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.real_run_rest = FactoryMainWindow._run_rest
        patcher = mock.patch.object(FactoryMainWindow, "_run_rest")
        self.run_rest = patcher.start()
        self.addCleanup(patcher.stop)
        self.window = FactoryMainWindow("http://127.0.0.1:65534")
        self.window.plc_page.stop()
        self.run_rest.reset_mock()

    def tearDown(self) -> None:
        self.window.plc_page.stop()
        self.window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()

    def test_module_selection_updates_config_and_invalidates_previous_preview(self) -> None:
        self.window.limb.setCurrentIndex(self.window.limb.findData("left_arm"))
        self.window._config_preview = {"limb": "left_arm"}
        self.window.configure_button.setEnabled(True)

        self.window.limb.setCurrentIndex(self.window.limb.findData("right_arm"))

        self.assertEqual(self.window._selected_limb(), "right_arm")
        self.assertEqual(self.window.config_path.text(), "right_arm_motors.yaml")
        self.assertEqual(self.window._config_preview, {})
        self.assertFalse(self.window.configure_button.isEnabled())
        self.assertFalse(self.window.apply_editor_button.isEnabled())
        self.run_rest.assert_not_called()

    def test_recording_and_run_limit_controls_update_editable_inputs(self) -> None:
        self.window.record.setChecked(False)
        self.assertFalse(self.window.record_rate_hz.isEnabled())
        self.window.record.setChecked(True)
        self.assertTrue(self.window.record_rate_hz.isEnabled())

        self.window.cycles_mode.click()
        self.assertTrue(self.window.cycles.isEnabled())
        self.assertFalse(self.window.duration_hours.isEnabled())
        self.window.duration_mode.click()
        self.assertFalse(self.window.cycles.isEnabled())
        self.assertTrue(self.window.duration_hours.isEnabled())

    def test_current_page_buttons_open_their_actual_dialogs(self) -> None:
        for button, dialog in (
            (self.window.module_config_button, self.window.module_config_dialog),
            (self.window.motor_monitor_button, self.window.motor_dialog),
            (self.window.zeroing_button, self.window.zero_dialog),
        ):
            with self.subTest(button=button.text()):
                self.assertFalse(dialog.isVisible())
                button.click()
                self.assertTrue(dialog.isVisible())
                dialog.hide()
        self.run_rest.assert_not_called()

    def test_playback_buttons_dispatch_the_matching_http_commands(self) -> None:
        for button, path in (
            (self.window.pause_button, "/api/v1/playback/pause"),
            (self.window.resume_button, "/api/v1/playback/resume"),
            (self.window.stop_button, "/api/v1/playback/stop"),
        ):
            with (
                self.subTest(path=path),
                mock.patch("factory_hmi.client.request.urlopen") as transport,
            ):
                transport.return_value.__enter__.return_value.read.return_value = b"{}"
                button.setEnabled(True)
                button.click()
                self.run_rest.call_args.args[1]()
                sent = transport.call_args.args[0]
                self.assertEqual(sent.method, "POST")
                self.assertEqual(sent.full_url, self.window.client.base_url + path)
                self.assertEqual(json.loads(sent.data), {})
                self.run_rest.reset_mock()

    def test_software_disable_requires_confirmation_before_sending(self) -> None:
        self.window.disable_button.setEnabled(True)
        with mock.patch(
            "factory_hmi.desktop.app.QMessageBox.warning",
            return_value=QMessageBox.StandardButton.Cancel,
        ):
            self.window.disable_button.click()
        self.run_rest.assert_not_called()

        with (
            mock.patch(
                "factory_hmi.desktop.app.QMessageBox.warning",
                return_value=QMessageBox.StandardButton.Yes,
            ),
            mock.patch.object(self.window.client, "disable_motors") as disable,
        ):
            self.window.disable_button.click()
            self.run_rest.call_args.args[1]()
            disable.assert_called_once_with()

    def test_snapshot_and_heartbeat_suppress_overlap_and_clear_pending_after_success_or_failure(self) -> None:
        self.run_rest.side_effect = lambda *args, **kwargs: self.real_run_rest(
            self.window, *args, **kwargs
        )
        for failed in (False, True):
            with (
                self.subTest(failed=failed),
                mock.patch.object(
                    self.window.client, "snapshot", return_value={},
                    side_effect=GatewayError("offline") if failed else None,
                ) as snapshot,
                mock.patch.object(
                    self.window.client, "lease_heartbeat", return_value={},
                    side_effect=GatewayError("offline") if failed else None,
                ) as heartbeat,
                mock.patch.object(self.window, "_snapshot_received"),
                mock.patch.object(self.window, "_rest_failed") as failure,
            ):
                self.window._lease_enabled = True
                for _ in range(2):
                    self.window._poll_snapshot()
                    self.window._send_lease_heartbeat()
                self.assertTrue(self.window._snapshot_pending)
                self.assertTrue(self.window._heartbeat_pending)
                self.window.pool.waitForDone(2000)
                deadline = time.monotonic() + 2
                while (
                    (self.window._snapshot_pending or self.window._heartbeat_pending)
                    and time.monotonic() < deadline
                ):
                    self.app.processEvents()
                snapshot.assert_called_once_with()
                heartbeat.assert_called_once_with()
                self.assertFalse(self.window._snapshot_pending)
                self.assertFalse(self.window._heartbeat_pending)
                self.assertFalse(self.window._workers)
                self.assertEqual(failure.call_count, 2 if failed else 0)

    def test_plc_start_submits_the_selected_channels_and_current_setpoints(self) -> None:
        page = self.window.plc_page
        page._snapshot = {"coordination": {"epoch": "test-epoch", "revision": 7}}
        self.window._platform_operator = "test-operator"
        page.voltage_setpoint.setValue(12.345)
        page.current_setpoint.setValue(1.234)
        for index, checkbox in enumerate(page.channel_checks):
            checkbox.setChecked(index in (0, 2))
        page.apply_setpoints_button.setEnabled(True)

        with mock.patch.object(self.window.client, "plc_start") as start:
            page.apply_setpoints_button.click()
            self.assertTrue(page._pending)
            operation = self.run_rest.call_args.args[1]
            page.voltage_setpoint.setValue(9.0)
            operation()
            start.assert_called_once_with([1, 3], "test-operator", 12.345, 1.234, context={
                "epoch": "test-epoch", "revision": 7,
                "request_id": page._submitted_request_id,
            })
            self.run_rest.call_args.kwargs["on_finished"]()
        self.assertFalse(page._pending)

    def test_plc_stop_and_channel_buttons_dispatch_without_reusing_the_last_action(self) -> None:
        page = self.window.plc_page
        self.window._platform_operator = "test-operator"
        for button, method, arguments in (
            (page.stop_button, "plc_stop", ("test-operator",)),
            (page.all_stop_button, "plc_all_stop", ("test-operator",)),
            (page.channel_buttons[2], "plc_set_channel", (3, True, "test-operator")),
        ):
            with (
                self.subTest(method=method),
                mock.patch.object(self.window.client, method) as command,
            ):
                button.setEnabled(True)
                button.click()
                self.run_rest.call_args.args[1]()
                self.assertEqual(command.call_args.args, arguments)
                self.assertTrue(command.call_args.kwargs["context"]["request_id"])
                self.run_rest.call_args.kwargs["on_finished"]()
                self.assertFalse(page._pending)
                self.run_rest.reset_mock()


class GatewayActionContractTests(unittest.TestCase):
    @mock.patch("factory_hmi.client.request.urlopen")
    def test_manual_review_and_retry_actions_send_their_actual_payloads(self, transport: mock.Mock) -> None:
        transport.return_value.__enter__.return_value.read.return_value = b"{}"
        client = GatewayClient("http://gateway.invalid", client_id="offline-test")
        actions = (
            (lambda: client.manual_move([1.0, -2.0], 3.5), "/api/v1/manual/move", {"targets_deg": [1.0, -2.0], "speed_deg_s": 3.5}),
            (client.manual_stop, "/api/v1/manual/stop", {}),
            (lambda: client.platform_login("operator", "test-password", station_id="MP-01"), "/api/v1/platform/login", {"username": "operator", "password": "test-password", "station_id": "MP-01"}),
            (lambda: client.retry_execution("record-1"), "/api/v1/executions/retry", {"test_id": "record-1"}),
            (lambda: client.rebuild_execution("record-1"), "/api/v1/executions/rebuild", {"test_id": "record-1"}),
            (lambda: client.plc_set_ps1_setpoints(12.3, 1.5, "operator", context={"epoch": "e1", "revision": 2, "request_id": "request-1"}), "/api/v1/plc/setpoints", {"voltage": 12.3, "current": 1.5, "user": "operator", "request_id": "request-1", "expected_epoch": "e1", "expected_revision": 2}),
        )
        for action, path, payload in actions:
            with self.subTest(path=path):
                action()
                sent = transport.call_args.args[0]
                self.assertEqual(sent.method, "POST")
                self.assertEqual(sent.full_url, client.base_url + path)
                self.assertEqual(json.loads(sent.data), payload)

        client.submit_execution("record-1", review={"verified_status": "pass", "operator_summary": "人工核实", "tester": "operator", "bench": "MP-01"})
        sent = transport.call_args.args[0]
        self.assertEqual(sent.method, "POST")
        self.assertEqual(sent.full_url, client.base_url + "/api/v1/executions/submit")
        payload = json.loads(sent.data)
        self.assertEqual({name: payload[name] for name in ("test_id", "verified_status", "operator_summary", "tester", "bench")}, {
            "test_id": "record-1", "verified_status": "pass", "operator_summary": "人工核实", "tester": "operator", "bench": "MP-01",
        })


if __name__ == "__main__":
    unittest.main()
