#!/usr/bin/env python3
"""Exercise PS1 keyboard editing and refresh offline, without any PLC connection."""
from __future__ import annotations

import argparse
from copy import deepcopy
import os
from pathlib import Path
import sys
import unittest

SOURCE = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--runtime-dir', type=Path, default=SOURCE / '.local-runtime')
args = parser.parse_args()
runtime = args.runtime_dir.resolve(strict=True)
if os.environ.get('RP1_SETPOINT_VERIFY_BOOTSTRAPPED') != '1':
    env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(SOURCE), str(runtime)]),
               LD_LIBRARY_PATH=str(runtime), QT_QPA_PLATFORM='offscreen',
               QT_QPA_PLATFORM_PLUGIN_PATH=str(runtime / 'PySide6/Qt/plugins'),
               RP1_SETPOINT_VERIFY_BOOTSTRAPPED='1')
    os.execve(sys.executable, [sys.executable, str(Path(__file__).resolve()), *sys.argv[1:]], env)

from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import QApplication
from factory_hmi.desktop.plc_page import PlcCabinetPage


class FakeClient:
    def __init__(self):
        self.calls = []
        self.client_id = "offline-input"

    def plc_snapshot(self):
        return {}

    def plc_start(self, channels, user, voltage, current, *, context=None):
        self.calls.append((channels, user, voltage, current))
        return {}


class InputTests(unittest.TestCase):
    def setUp(self):
        self.client = FakeClient()
        self.queued = []
        self.operator = 'offline-operator'

        def runner(label, operation, receive, **kwargs):
            if label != '刷新 PLC 状态':
                self.queued.append(operation)
            finished = kwargs.get('on_finished')
            if finished:
                finished()

        self.page = PlcCabinetPage(self.client, runner, lambda: self.operator)
        self.page.stop()
        self.snapshot = dict(
            communication_state='LIVE', stale=False, phase='OFF', driver='mock',
            status=dict(ProtocolVersion=0x0102, PS1CommOK=True,
                        PS1SetpointValid=True, PS1SetVoltage=3., PS1SetCurrent=.5),
            feedback={},
            setpoint_limits=dict(enabled=True, voltage_min_v=.001, voltage_max_v=60.,
                                 current_min_a=.001, current_max_a=60., step=.001),
        )
        self.refresh()
        self.page.show()
        QApplication.processEvents()
        self.page._pending = False

    def tearDown(self):
        self.page.close()
        self.page.deleteLater()
        QApplication.processEvents()

    def refresh(self):
        self.page._receive(deepcopy(self.snapshot))

    def type_value(self, editor, text):
        editor.setFocus()
        editor.selectAll()
        for char in text:
            for event_type in (QEvent.Type.KeyPress, QEvent.Type.KeyRelease):
                QApplication.sendEvent(editor, QKeyEvent(event_type, ord(char), Qt.KeyboardModifier.NoModifier, char))
            before = editor.text()
            for _ in range(3):
                self.refresh()
            self.assertEqual(editor.text(), before, 'PLC polling changed a typed draft')

    def feedback(self, state):
        self.snapshot['feedback']['Setpoints'] = dict(state=state, detail='offline test')
        self.refresh()

    def test_keyboard_drafts_survive_refresh_and_focus_changes(self):
        self.type_value(self.page.voltage_setpoint, '12.345')
        self.type_value(self.page.current_setpoint, '1.234')
        self.page.apply_setpoints_button.setFocus()
        QApplication.processEvents()
        for _ in range(20):
            self.refresh()
        self.assertAlmostEqual(self.page.voltage_setpoint.value(), 12.345)
        self.assertAlmostEqual(self.page.current_setpoint.value(), 1.234)
        self.assertEqual(self.client.calls, [])
        self.assertEqual(self.queued, [])

    def test_first_ps1_readback_keeps_an_early_draft(self):
        self.page.close()
        self.page = PlcCabinetPage(self.client, lambda *a, **k: None, lambda: self.operator)
        self.page.stop()
        self.snapshot['status']['PS1CommOK'] = False
        self.snapshot['status']['PS1SetpointValid'] = False
        self.refresh()
        self.page.show()
        QApplication.processEvents()
        self.type_value(self.page.voltage_setpoint, '9.876')
        self.page.voltage_setpoint.interpretText()
        self.page.current_setpoint.setFocus()
        self.snapshot['status']['PS1CommOK'] = True
        self.snapshot['status']['PS1SetpointValid'] = True
        self.refresh()
        self.assertAlmostEqual(self.page.voltage_setpoint.value(), 9.876)

    def test_apply_commits_text_and_captures_values_before_worker_execution(self):
        self.type_value(self.page.voltage_setpoint, '12.345')
        self.type_value(self.page.current_setpoint, '1.234')
        self.page._apply_setpoints()
        self.assertEqual(len(self.queued), 1)
        self.assertEqual(self.client.calls, [])
        self.page.voltage_setpoint.setValue(7.)
        self.page.current_setpoint.setValue(.7)
        self.operator = 'changed-after-queue'
        self.queued[0]()
        self.assertEqual(self.client.calls[0][1:], ('offline-operator', 12.345, 1.234))

    def test_step_button_values_survive_polling(self):
        self.page.voltage_setpoint.stepUp()
        self.page.current_setpoint.stepUp()
        self.refresh()
        self.assertAlmostEqual(self.page.voltage_setpoint.value(), 3.001)
        self.assertAlmostEqual(self.page.current_setpoint.value(), .501)

    def test_external_confirmation_keeps_unsubmitted_edits(self):
        self.type_value(self.page.voltage_setpoint, '8.765')
        self.page.voltage_setpoint.interpretText()
        self.page.apply_setpoints_button.setFocus()
        self.feedback('pending')
        self.feedback('confirmed')
        self.assertAlmostEqual(self.page.voltage_setpoint.value(), 8.765)

    def test_confirmation_keeps_edits_made_after_submission(self):
        self.type_value(self.page.voltage_setpoint, '8.765')
        self.page._apply_setpoints()
        self.page.voltage_setpoint.setValue(9.876)
        self.page.apply_setpoints_button.setFocus()
        self.feedback('pending')
        self.feedback('confirmed')
        self.assertAlmostEqual(self.page.voltage_setpoint.value(), 9.876)

    def test_acknowledged_submission_still_syncs_plc_readback(self):
        self.type_value(self.page.voltage_setpoint, '8.765')
        self.page._apply_setpoints()
        self.page.apply_setpoints_button.setFocus()
        self.feedback('pending')
        self.snapshot['status']['PS1SetVoltage'] = 8.765
        self.feedback('confirmed')
        self.snapshot['status']['PS1SetVoltage'] = 7.654
        self.refresh()
        self.assertAlmostEqual(self.page.voltage_setpoint.value(), 7.654)

    def test_readonly_stale_running_and_pending_interlocks_remain(self):
        for changes in [dict(read_only=True), dict(stale=True), dict(phase='RUNNING'),
                        dict(feedback={'Setpoints': dict(state='pending')})]:
            with self.subTest(changes=changes):
                baseline = deepcopy(self.snapshot)
                self.snapshot.update(changes)
                self.refresh()
                self.assertFalse(self.page.voltage_setpoint.isEnabled())
                self.assertFalse(self.page.current_setpoint.isEnabled())
                self.assertFalse(self.page.apply_setpoints_button.isEnabled())
                self.snapshot = baseline


if __name__ == '__main__':
    app = QApplication([])
    unittest.main(argv=[sys.argv[0]], verbosity=2)
