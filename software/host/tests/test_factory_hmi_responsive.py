from __future__ import annotations

import os
import unittest
from unittest import mock


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QTimer
from PySide6.QtWebSockets import QWebSocket
from PySide6.QtWidgets import QApplication

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
        self.window = FactoryMainWindow("http://127.0.0.1:65534")

    def tearDown(self) -> None:
        self.window.deleteLater()

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
