"""Recording runs using fake motors and a local data root, never real PLC/CAN."""
import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest
from jsonschema import Draft202012Validator, FormatChecker

from factory_hmi.core.identity import build_local_execution_code
from factory_hmi.core.service import FactoryService
from factory_hmi.gateway.schemas import PlaybackStartRequest

HMI = Path(__file__).resolve().parents[1]


@pytest.fixture
def service(tmp_path, monkeypatch):
    monkeypatch.delenv('RP1_FACTORY_BENCH_ID', raising=False)
    monkeypatch.delenv('RP1_FACTORY_STATION_ID', raising=False)
    result = FactoryService(data_root=tmp_path/'data')
    result.configure(config_path=str(HMI/'scripts/config/arm_motors.yaml'),limb='left_arm',backend='fake')
    trajectory = tmp_path/'offline.npz'
    np.savez(trajectory,motor_pos=np.zeros((31,7)),fps=np.asarray([100.]),time=np.arange(31)/100.,limb=np.asarray(['left_arm']))
    result.import_trajectory(str(trajectory))
    result.connect()  # FakeMotorBackend only; fake CAN state, no socket or native SDK.
    result.arm()
    yield result
    result.controller.wait(timeout=3)
    with result._lock:
        threads = list(result._report_threads)
    for thread in threads:
        thread.join(timeout=5)
    result.disconnect()


def finish_report(service):
    assert service.controller.wait(timeout=3)
    with service._lock:
        threads = list(service._report_threads)
    for thread in threads:
        thread.join(timeout=5)
        assert not thread.is_alive()
    assert service._report_errors == {}
    assert service.controller.snapshot()['playback']['progress']['outcome'] == 'completed'
    history = service.execution_history()
    assert len(history) == 1
    return service.report_manager.manifest(history[0]['test_id'])


def platform_context():
    return {'test_case_id':'REL-SARM-001', **{key:'11111111-1111-4111-8111-111111111111' for key in (
        'campaign_id','cycle_id','segment_id','asset_id','configuration_id','test_case_version_id','station_id'
    )}}


def verified_review():
    return {'tester':'实际测试人','bench':'MP-01','station_id':'11111111-1111-4111-8111-111111111111','outcome':'成功','notes':'无','discrepancy_reason':'依据本次合成测试记录人工核实','test_name':'合成测试','actual_object':'RP1.3-SARM-001'}


@pytest.mark.parametrize('scope',[None,'local'])
def test_recording_without_bench_or_platform_login_finishes_csv_and_local_report(service, scope):
    service.start(test_case_id='left_arm',record=True,recording_scope=scope,robot_id='rp1-arm-03',loop=False,record_rate_hz=10)
    manifest=finish_report(service)
    assert manifest['test_id'].startswith('LOCAL_left_arm_')
    assert manifest['recording_scope']=='local'
    assert manifest['sample_id']=='rp1-arm-03'
    assert manifest['bench_id']=='unknown'
    assert service.record_file().is_file()
    assert len(service.record_file().read_text().splitlines()) >= 2
    assert service.report_manager.report_path(manifest['test_id']).is_file()
    schema=json.loads((HMI/'factory_hmi/contracts/execution-bundle-v1.schema.json').read_text())
    Draft202012Validator(schema,format_checker=FormatChecker()).validate(manifest)
    rebuilt=service.report_manager.rebuild(manifest['test_id'])
    assert rebuilt.manifest['recording_scope']=='local'
    assert service.execution_history()[0]['recording_scope']=='local'
    with pytest.raises(ValueError,match='必须核实'):
        service.submit_execution(manifest['test_id'])
    assert service.outbox.list()==[]


def test_local_station_login_label_is_not_fabricated_as_a_bench_id(service):
    service.start(test_case_id='left_arm',record=True,recording_scope='local',robot_id='local sample',station_id='Fab_01',operator_id='operator',loop=False)
    manifest=finish_report(service)
    assert manifest['bench_id']=='unknown'
    assert manifest['station_id']=='Fab_01'
    assert manifest['recording_scope']=='local'


@pytest.mark.parametrize('changes,reason',[
    ({'bench_id':None},'bench_id must match'),
    ({'sample':'arm local'},'sample_id must match'),
    ({'cycle_id':None},'上下文不完整'),
    ({'station_id':'Fab_01'},'必须是真实上下文的 UUID'),
])
def test_platform_record_rejects_invalid_identity_or_partial_context_before_motion(service,changes,reason):
    kwargs={**platform_context(),'bench_id':'MP-01','robot_id':'RP1.3-SARM-001'}
    changes=dict(changes)
    if 'sample' in changes:kwargs['robot_id']=changes.pop('sample')
    kwargs.update(changes)
    with patch.object(service.controller,'start') as start:
        with pytest.raises(ValueError,match=reason):
            service.start(record=True,recording_scope='platform',**kwargs)
        start.assert_not_called()
    assert service.controller.enable_recording is False
    assert not list(service.record_root.glob('*'))


def test_partial_platform_context_does_not_silently_fall_back_to_local(service):
    with patch.object(service.controller,'start') as start:
        with pytest.raises(ValueError,match='上下文不完整'):
            service.start(test_case_id='REL-SARM-001',record=True,robot_id='RP1.3-SARM-001',campaign_id=platform_context()['campaign_id'])
        start.assert_not_called()


def test_local_request_cannot_smuggle_platform_associations(service):
    with pytest.raises(ValueError,match='本地记录不能同时携带平台测试上下文'):
        service.start(test_case_id='REL-SARM-001',record=True,recording_scope='local',campaign_id=platform_context()['campaign_id'])


def test_complete_platform_context_preserves_canonical_identity_and_independent_submission(service):
    service.start(record=True,recording_scope='platform',robot_id='RP1.3-SARM-001',bench_id='MP-01',loop=False,**platform_context())
    official = 'REL-SARM-001_2610101800_RP1.3-SARM-001_MP-01_E42'
    service.adopt_platform_execution_code(official)
    manifest=finish_report(service)
    assert manifest['test_id'].startswith('LOCAL_REL-SARM-001_')
    assert manifest['execution_code'] == official
    assert service.execution_history()[0]['execution_code'] == official
    rebuilt = service.report_manager.rebuild(manifest['test_id'])
    assert rebuilt.manifest['execution_code'] == official
    assert manifest['recording_scope']=='platform'
    from factory_hmi.sync.presence import build_presence_snapshot
    snapshot = service.snapshot()
    assert snapshot['playback']['execution_uuid'] == manifest['execution_uuid']
    assert snapshot['platform']['execution_uuid'] == manifest['execution_uuid']
    assert build_presence_snapshot({'session': snapshot}, {})['runs'][0]['execution_uuid'] == manifest['execution_uuid']
    assert service.outbox.list()==[]  # Completing a run does not submit it.
    queued=service.submit_execution(manifest['test_id'], review=verified_review())
    assert queued['state']=='local_completed'  # Local queue, no network or approval.


def test_unrecorded_run_still_needs_no_identity_or_record_files(service):
    service.start(test_case_id='REL-SARM-001',record=False,loop=False)
    assert service.controller.wait(timeout=3)
    assert service.controller.enable_recording is False
    assert service.controller.record_output is None
    assert not list(service.record_root.glob('*'))


def test_platform_runs_in_same_minute_keep_distinct_files(service):
    class FixedTime(datetime):
        @classmethod
        def now(cls, tz=None):
            value = datetime(2026, 10, 10, 10, 0, tzinfo=timezone.utc)
            return value.astimezone(tz) if tz is not None else value.replace(tzinfo=None)
    manifests = []
    with patch('factory_hmi.core.service.datetime', FixedTime):
        for index in range(2):
            if index:
                service.prepare_next_run()
                service.import_trajectory(str(service.data_root.parent / 'offline.npz'))
                service.arm()
            snapshot = service.start(record=True, recording_scope='platform', robot_id='RP1.3-SARM-001', bench_id='MP-01', loop=False, **platform_context())
            assert snapshot['playback']['execution_code'] == ''
            assert snapshot['playback']['test_id'].startswith('LOCAL_REL-SARM-001_')
            assert service.controller.wait(timeout=3)
            with service._lock:
                threads = list(service._report_threads)
            for thread in threads:
                thread.join(timeout=5)
            assert service._report_errors == {}
            manifests.append(service.report_manager.manifest(snapshot['playback']['test_id']))
    first, second = manifests
    assert first['execution_code'] == second['execution_code'] == ''
    assert first['test_id'] != second['test_id']
    assert first['test_id'].startswith('LOCAL_') and second['test_id'].startswith('LOCAL_')
    assert len(list(service.record_root.glob('*.csv'))) == 2
    assert len(service.execution_history()) == 2


def test_execution_history_retains_server_number_and_local_actions(service):
    service.start(record=True, recording_scope='platform', robot_id='RP1.3-SARM-001', bench_id='MP-01', loop=False, **platform_context())
    official = 'REL-SARM-001_2610101800_RP1.3-SARM-001_MP-01_E42'
    service.adopt_platform_execution_code(official)
    manifest = finish_report(service)
    queued = service.submit_execution(manifest['test_id'], review=verified_review())
    service.outbox.update(queued['execution_uuid'], state='uploaded', execution_code=official)
    item, = service.execution_history()
    assert item['execution_code'] == official
    assert item['execution_code_confirmed']
    assert item['test_id'] == manifest['test_id']
    assert service.execution_report_file(item['test_id']).is_file()
    assert service.retry_submission(item['test_id'])['state'] == 'uploaded'


def test_local_ids_in_same_minute_are_unique_and_safe_for_file_paths():
    from datetime import datetime,timezone
    now=datetime(2026,10,10,8,0,tzinfo=timezone.utc)
    first=build_local_execution_code('../left arm',now,'11111111-1111-4111-8111-111111111111')
    second=build_local_execution_code('../left arm',now,'22222222-2222-4222-8222-222222222222')
    assert first!=second
    assert '/' not in first and '\\' not in first
    assert first.startswith('LOCAL_left_arm_')


def test_invalid_record_scope_is_rejected_by_api_schema():
    from pydantic import ValidationError
    with pytest.raises(ValidationError):PlaybackStartRequest(recording_scope='guess')


def test_core_cannot_bypass_required_case_and_other_name_before_motion(service):
    with patch.object(service.controller, 'start') as start:
        with pytest.raises(ValueError, match='用例编号必填'):
            service.start(record=False)
        with pytest.raises(ValueError, match='临时测试名称'):
            service.start(test_case_id='OTHER', robot_id='合成对象', bench_id='A10')
        with pytest.raises(ValueError, match='实际测试对象和工位'):
            service.start(test_case_id='OTHER', test_name='临时测试', robot_id='合成对象')
        start.assert_not_called()


def test_gateway_record_checkbox_request_starts_and_local_submit_is_refused(service,tmp_path,monkeypatch):
    from fastapi.testclient import TestClient
    from factory_hmi.gateway.app import create_app
    mock_config=tmp_path/'mock-plc.yaml'
    import yaml
    mock_settings=yaml.safe_load((HMI/'factory_hmi/config/plc_cabinet.yaml').read_text())
    mock_settings['driver']='mock'
    mock_settings['access_mode']='control'
    mock_config.write_text(yaml.safe_dump(mock_settings))
    monkeypatch.setenv('RP1_FACTORY_PLC_CONFIG',str(mock_config))
    with TestClient(create_app(controller=service,data_root=tmp_path/'gateway')) as client:
        headers={'X-RP1-Client-ID':'offline-recorder'}
        response=client.post('/api/v1/playback/start',headers=headers,json={
            'record':True,'recording_scope':'local','record_rate_hz':10,
            'robot_id':'现场机械臂1','loop':False,'test_case_id':'OTHER','test_name':'合成测试','bench_id':'A10',
        })
        assert response.status_code==200,response.text
        manifest=finish_report(service)
        refused=client.post('/api/v1/executions/submit',headers=headers,json={'test_id':manifest['test_id']})
        assert refused.status_code==409,refused.text
        assert '请先登录' in refused.text
        assert service.outbox.list()==[]
