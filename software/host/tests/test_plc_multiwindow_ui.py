"""Two real Qt pages using one mock cabinet service; no sockets or real devices."""
from copy import deepcopy
from pathlib import Path
import pytest

from PySide6.QtWidgets import QApplication

from factory_hmi.client import GatewayError
from factory_hmi.desktop.plc_page import PlcCabinetPage
from factory_hmi.plc.client import MockPlcClient
from factory_hmi.plc.config import load_plc_config
from factory_hmi.plc.coordination import PlcRequestConflict
from factory_hmi.plc.service import PlcCabinetService


class OfflineClient:
    def __init__(self, service, client_id):
        self.service, self.client_id = service, client_id
    def plc_snapshot(self): return self.service.snapshot()
    def command(self, op, payload, user, ctx):
        try:
            return self.service.coordination.execute(op, payload, user=user, client_id=self.client_id,
                request_id=ctx['request_id'], expected_epoch=ctx.get('epoch'), expected_revision=ctx.get('revision'))
        except PlcRequestConflict as exc:
            raise GatewayError('PLC conflict', status_code=409, detail={
                'code': exc.code, 'message': str(exc), 'snapshot': self.service.snapshot(),
            }) from exc
    def plc_start(self, channels, user, voltage, current, *, context):
        return self.command('start', dict(channels=channels, voltage=voltage, current=current), user, context)
    def plc_all_stop(self, user, *, context): return self.command('all-stop', {}, user, context)
    def plc_set_channel(self, channel, enabled, user, *, context):
        return self.command('channel', dict(channel=channel, enabled=enabled), user, context)


class Runner:
    def __init__(self): self.jobs = []
    def __call__(self, label, op, success, **kwargs):
        if label == '刷新 PLC 状态':
            success(op())
            kwargs['on_finished']()
        else:
            self.jobs.append((op, success, kwargs))
    def finish(self, index=0):
        op, success, kwargs = self.jobs.pop(index)
        try: success(op())
        except Exception as exc: kwargs['on_failure'](exc)
        finally: kwargs['on_finished']()


@pytest.fixture(scope="session")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def pages(tmp_path, monkeypatch, qapp):
    monkeypatch.setattr('factory_hmi.plc.service.build_plc_client', lambda *a: MockPlcClient())
    service = PlcCabinetService(load_plc_config(), data_root=tmp_path)
    service.controller.connect(user='offline')
    runners = [Runner(), Runner()]
    pages = [PlcCabinetPage(OfflineClient(service, f'window-{i}'), runners[i], lambda: 'offline') for i in range(2)]
    for page in pages:
        page.stop()
        page._receive(service.snapshot())
    yield service, pages, runners
    for page in pages:
        page.stop(); page.close(); page.deleteLater()
    service.close()


def complete(service):
    for _ in range(30):
        service.controller.tick()
        snap = service.snapshot()
        if not snap['coordination']['busy']: return snap
    raise AssertionError(snap)


def test_both_pages_show_same_plc_result_and_identify_the_originating_window(pages):
    service, (a, b), (ra, rb) = pages
    a._start(); ra.finish()
    final = complete(service)
    a._receive(final); b._receive(deepcopy(final))
    assert a._snapshot['phase'] == b._snapshot['phase'] == 'RUNNING'
    assert a._snapshot['command'] == b._snapshot['command']
    assert '本窗口' in a.coordination_state.text()
    assert '其他窗口' in b.coordination_state.text()
    b._toggle_channel(1); rb.finish()
    final = complete(service)
    a._receive(final); b._receive(deepcopy(final))
    assert a._snapshot['status']['ChannelPermit'][0] is False
    assert a._snapshot['status'] == b._snapshot['status']
    assert '其他窗口' in a.coordination_state.text()
    assert '本窗口' in b.coordination_state.text()


def test_stale_page_conflict_refreshes_state_without_overwriting_active_command(pages):
    service, (a, b), (ra, rb) = pages
    a._start(); ra.finish(); complete(service)
    a._receive(service.snapshot())
    b._start(); rb.finish()  # B still sees the old OFF page/revision.
    assert '其他窗口已改变' in b._request_error
    assert b._snapshot['phase'] == 'RUNNING'
    assert b._snapshot['coordination']['last_command']['client_id'] == 'window-0'
    assert b._snapshot['feedback']['Start']['state'] == 'confirmed'


def test_late_poll_cannot_roll_back_a_new_command_response(pages):
    service, (a, _), (ra, _) = pages
    old = deepcopy(a._snapshot)
    a._start(); ra.finish()
    a._receive(complete(service))
    a._receive(old)
    assert a._snapshot['phase'] == 'RUNNING'
    assert not a.start_button.isEnabled()


def test_all_stop_is_submitted_during_local_pending_and_blocks_a_late_start(pages):
    service, (a, _), (ra, _) = pages
    a._start()  # Ordinary request queued on the worker, not executed yet.
    assert a._pending_count == 1
    a.all_stop_button.click()
    assert a._pending_count == 2 and len(ra.jobs) == 2
    ra.finish(1)  # AllStop arrives first.
    assert a._pending and a._pending_count == 1
    ra.finish(0)  # Delayed startup is rejected, not replayed.
    assert not a._pending and a._pending_count == 0
    a._receive(complete(service))
    assert a._snapshot['command']['MainEnable'] is False
    assert a._snapshot['command']['PS1OutputEnable'] is False
    assert a._snapshot['phase'] == 'FAULT'


def test_foreign_confirmation_cannot_clear_a_rejected_local_setpoint_draft(pages):
    service, (a, b), (ra, rb) = pages
    a._start(); ra.finish(); complete(service)
    b.voltage_setpoint.setValue(12.345)
    b._start(); rb.finish()  # rejected stale revision
    assert b._setpoint_dirty
    b._receive(service.snapshot())
    assert b._setpoint_dirty
    assert b.voltage_setpoint.value() == 12.345
