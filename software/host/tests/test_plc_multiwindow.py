"""Multi-window arbitration against mock PLCs only; no control-network access."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
import threading
import uuid

from fastapi.testclient import TestClient
import pytest

from factory_hmi.gateway.app import create_app
from factory_hmi.plc.client import MockPlcClient, PlcCommunicationError
from factory_hmi.plc.config import load_plc_config
from factory_hmi.plc.coordination import PlcRequestConflict
from factory_hmi.plc.models import CabinetPhase
from factory_hmi.plc.service import PlcCabinetService, PlcControlDisabledError


@pytest.fixture
def service(tmp_path, monkeypatch):
    client = MockPlcClient()
    monkeypatch.setattr('factory_hmi.plc.service.build_plc_client', lambda *a: client)
    service = PlcCabinetService(load_plc_config(), data_root=tmp_path)
    service.controller.connect(user='offline')
    yield service
    service.close()


def context(service):
    return service.snapshot()['coordination']


def execute(service, operation, payload=None, *, source='window-a', request_id=None, seen=None):
    seen = seen or context(service)
    return service.coordination.execute(
        operation, payload or {}, user=source, client_id=source,
        request_id=request_id or str(uuid.uuid4()),
        expected_epoch=seen['epoch'], expected_revision=seen['revision'],
    )


def advance(service, phase=None):
    for _ in range(30):
        service.controller.tick()
        snap = service.snapshot()
        if not snap['coordination']['busy'] and (phase is None or snap['phase'] == phase):
            return snap
    raise AssertionError(snap)


def start(service):
    execute(service, 'start', {'channels': [1, 2], 'voltage': 3., 'current': .5})
    return advance(service, 'RUNNING')


def test_simultaneous_setpoints_accept_exactly_one_without_overwriting_feedback(service):
    seen = context(service)
    gate = threading.Barrier(2)
    before = len(service.client.command_history)
    def submit(source, voltage):
        gate.wait()
        try:
            return execute(service, 'setpoints', {'voltage': voltage, 'current': 1.}, source=source, seen=seen)
        except PlcRequestConflict as exc:
            return exc
    with ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(submit, 'window-a', 12.)
        b = pool.submit(submit, 'window-b', 24.)
        results = [a.result(), b.result()]
    assert sum(isinstance(r, dict) for r in results) == 1
    rejected = next(r for r in results if isinstance(r, PlcRequestConflict))
    assert rejected.code == 'stale_revision'
    assert len(service.client.command_history) == before + 1
    snap = service.snapshot()
    assert snap['feedback']['Setpoints']['state'] == 'pending'
    assert snap['coordination']['active_command']['state'] == 'pending'
    final = advance(service, 'OFF')
    assert final['coordination']['last_command']['state'] == 'confirmed'
    assert final['status']['PS1SetVoltage'] in {12., 24.}


def test_current_revision_still_rejects_busy_and_never_queues(service):
    execute(service, 'setpoints', {'voltage': 12., 'current': 1.})
    before = len(service.client.command_history)
    with pytest.raises(PlcRequestConflict, match='正在执行') as error:
        execute(service, 'setpoints', {'voltage': 24., 'current': 2.}, source='window-b')
    assert error.value.code == 'busy'
    snap = advance(service, 'OFF')
    assert snap['status']['PS1SetVoltage'] == 12.
    assert len(service.client.command_history) == before


def test_start_during_pending_setpoints_has_no_partial_start_mutation(service):
    execute(service, 'setpoints', {'voltage': 12., 'current': 1.})
    assert service.controller._start_requested is False
    with pytest.raises(PlcRequestConflict):
        service.start_sequence([1], user='window-b', voltage=24., current=2.)
    assert service.controller._start_requested is False
    assert service.controller.feedback['Setpoints'].state.value == 'pending'
    assert advance(service)['phase'] == 'OFF'


def test_same_request_is_idempotent_before_and_after_acknowledgement(service):
    seen = context(service)
    key = str(uuid.uuid4())
    first = execute(service, 'setpoints', {'voltage': 12., 'current': 1.}, seen=seen, request_id=key)
    before = len(service.client.command_history)
    repeat = execute(service, 'setpoints', {'voltage': 12., 'current': 1.}, seen=seen, request_id=key)
    assert repeat['replayed'] and repeat['request']['request_id'] == first['request']['request_id']
    advance(service)
    final = execute(service, 'setpoints', {'voltage': 12., 'current': 1.}, seen=seen, request_id=key)
    assert final['request']['state'] == 'confirmed'
    assert len(service.client.command_history) == before
    with pytest.raises(PlcRequestConflict) as error:
        execute(service, 'setpoints', {'voltage': 24., 'current': 1.}, seen=seen, request_id=key)
    assert error.value.code == 'request_id_reused'


def test_each_window_can_change_its_channel_without_clobbering_other_channel(service):
    start(service)
    execute(service, 'channel', {'channel': 1, 'enabled': False}, source='window-a')
    first = advance(service, 'RUNNING')
    assert first['status']['ChannelPermit'][:2] == [False, True]
    execute(service, 'channel', {'channel': 2, 'enabled': False}, source='window-b')
    final = advance(service, 'RUNNING')
    assert final['command']['ChannelEnable'] == [False] * 4
    assert final['status']['ChannelPermit'] == [False] * 4


def test_opposite_channel_request_from_stale_window_is_not_applied(service):
    start(service)
    seen = context(service)
    execute(service, 'channel', {'channel': 1, 'enabled': False}, seen=seen)
    advance(service)
    before = len(service.client.command_history)
    with pytest.raises(PlcRequestConflict):
        execute(service, 'channel', {'channel': 1, 'enabled': True}, source='window-b', seen=seen)
    assert len(service.client.command_history) == before
    assert service.controller.command.channel_enable[0] is False


def test_channel_receipt_waits_for_actual_output_not_just_sequence_ack(service):
    start(service)
    execute(service, 'channel', {'channel': 1, 'enabled': False})
    service.client.set_status(channel_permit=(True, True, False, False))
    for _ in range(3):
        service.controller.tick()
    snap = service.snapshot()
    assert snap['coordination']['busy']
    assert snap['coordination']['active_command']['state'] == 'pending'
    service.client.set_status(channel_permit=(False, True, False, False))
    assert advance(service)['coordination']['last_command']['state'] == 'confirmed'


def test_other_window_all_stop_supersedes_start_even_from_old_revision(service):
    seen = context(service)
    execute(service, 'start', {'channels': [1, 2], 'voltage': 3., 'current': .5}, seen=seen)
    receipt = execute(service, 'all-stop', source='window-b', seen=seen)
    assert receipt['request']['client_id'] == 'window-b'
    snap = advance(service)
    records = snap['coordination']['commands']
    assert records[0]['state'] == 'superseded'
    assert records[-1]['state'] == 'confirmed'
    assert snap['feedback']['Start']['state'] != 'pending'
    assert not any(snap['command']['ChannelEnable'])
    assert snap['command']['MainEnable'] is False
    assert snap['command']['PS1OutputEnable'] is False
    # No queued startup or later delayed write can revive the cabinet.
    for _ in range(5):
        service.controller.tick()
    assert service.controller.phase == CabinetPhase.FAULT
    assert service.controller.command.main_enable is False


def test_all_stop_waits_for_actual_power_off(service):
    start(service)
    execute(service, 'all-stop', source='window-b')
    service.client.set_status(ps1_actual_output=True, main_ready=True)
    service.controller.tick()
    assert service.snapshot()['coordination']['busy']
    service.client.set_status(ps1_actual_output=False, main_ready=False)
    assert advance(service)['coordination']['last_command']['state'] == 'confirmed'


def test_missing_context_and_old_epoch_cannot_bypass_arbitration(service):
    before = len(service.client.command_history)
    for request_id, epoch, revision in [(None, None, None), ('x', 'previous-gateway', 0)]:
        with pytest.raises(PlcRequestConflict):
            service.coordination.execute('setpoints', {'voltage': 24., 'current': 1.}, user='legacy', client_id='legacy', request_id=request_id, expected_epoch=epoch, expected_revision=revision)
    assert len(service.client.command_history) == before


def test_readonly_applies_to_both_windows_and_all_stop(service):
    service.read_only = True
    for source in ['window-a', 'window-b']:
        with pytest.raises(PlcControlDisabledError):
            execute(service, 'all-stop', source=source)


def test_api_both_windows_receive_shared_state_and_conflict_snapshot(tmp_path):
    app = create_app(data_root=tmp_path)
    with TestClient(app) as client:
        snapshot = client.get('/api/v1/plc/snapshot').json()
        ctx = snapshot['coordination']
        body = dict(user='a', request_id='a-1', expected_epoch=ctx['epoch'], expected_revision=ctx['revision'], voltage=12., current=1.)
        first = client.post('/api/v1/plc/setpoints', headers={'X-RP1-Client-ID': 'window-a'}, json=body)
        assert first.status_code == 200
        body.update(user='b', request_id='b-1', voltage=24.)
        second = client.post('/api/v1/plc/setpoints', headers={'X-RP1-Client-ID': 'window-b'}, json=body)
        assert second.status_code == 409
        shared = second.json()['detail']['snapshot']
        assert shared['coordination']['last_command']['client_id'] == 'window-a'
        assert shared['feedback']['Setpoints']['state'] in {'pending', 'confirmed'}
        assert shared['command']['SetVoltage'] == 12.
        unguarded = client.post('/api/v1/plc/setpoints', json={'user': 'legacy', 'voltage': 30., 'current': 1.})
        assert unguarded.status_code == 409


def test_snapshot_sequence_increases_without_invalidating_control_revision(service):
    first = service.snapshot()['coordination']
    second = service.snapshot()['coordination']
    assert first['revision'] == second['revision']
    assert second['snapshot_sequence'] > first['snapshot_sequence']


def test_channel_change_is_rejected_while_off_without_false_running_state(service):
    before = len(service.client.command_history)
    with pytest.raises(PlcRequestConflict):
        execute(service, 'channel', {'channel': 1, 'enabled': False})
    assert service.controller.phase == CabinetPhase.OFF
    assert len(service.client.command_history) == before


def test_individual_channel_feedback_tracks_shared_transaction(service):
    start(service)
    execute(service, 'channel', {'channel': 1, 'enabled': False})
    assert service.snapshot()['feedback']['ChannelEnable1']['state'] == 'pending'
    snap = advance(service)
    assert snap['feedback']['ChannelEnable1']['state'] == 'confirmed'
    assert snap['coordination']['last_command']['state'] == 'confirmed'


def test_uncertain_write_is_cached_and_never_repeated(service, monkeypatch):
    original = service.client.write_command
    def uncertain(command):
        original(command)
        raise PlcCommunicationError('reply lost after write')
    monkeypatch.setattr(service.client, 'write_command', uncertain)
    seen = context(service)
    before = len(service.client.command_history)
    for _ in range(2):
        with pytest.raises(PlcRequestConflict) as error:
            execute(service, 'setpoints', {'voltage': 24., 'current': 1.}, request_id='one-write', seen=seen)
        assert error.value.code == 'communication_error'
    assert len(service.client.command_history) == before + 1
    assert service.snapshot()['communication_state'] == 'COMM_LOST'


def test_lost_communication_settles_start_feedback_and_receipt(service):
    execute(service, 'start', {'channels': [1], 'voltage': 3., 'current': .5})
    service.controller._communication_lost('offline test disconnect')
    snap = service.snapshot()
    assert snap['feedback']['Start']['state'] != 'pending'
    assert snap['coordination']['last_command']['state'] in {'rejected', 'failed'}


def test_channel_actual_confirmation_timeout_enters_safe_fault(service):
    start(service)
    execute(service, 'channel', {'channel': 1, 'enabled': False})
    service.client.set_status(channel_permit=(True, True, False, False))
    for _ in range(3): service.controller.tick()
    service.coordination._active['_deadline'] = 0
    snap = service.snapshot()
    assert snap['coordination']['last_command']['state'] == 'timeout'
    assert snap['phase'] == 'FAULT'
    assert snap['command']['AllStop'] is True
    assert service.controller.phase == CabinetPhase.FAULT
    assert service.controller.command.all_stop
