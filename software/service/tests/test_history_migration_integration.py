"""Authorized history acceptance; only a disposable database may run this test."""
import json
import os
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from test_hmi_presence_integration import environment, signed_in
from app.repositories.mtbf import MtbfRepository
from app.repositories.read_models import ReadModelRepository
from app.schemas.mtbf import CampaignMtbfConfigRequest
from app.services.worker import _interval_rows, claim_job, process_job

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'database'))
from imports.execution_csv import import_execution_history, load_csv

pytestmark = [pytest.mark.integration, pytest.mark.skipif(not os.environ.get('HISTORY_CSV'), reason='authorized local input required')]
REASON = 'pc3 isolated history acceptance'
TABLES = ('test.asset', 'test.test_campaign', 'test.station', 'test.test_execution', 'test.execution_result', 'test.runtime_interval', 'integration.execution_import_record', 'audit.change_log')


def set_context(conn, actor):
    conn.execute(text('SELECT iam.set_request_context(:actor,:request,:reason)'), {'actor':actor, 'request':'pc3-isolated-history', 'reason':REASON})


def counts(conn):
    return {name:conn.execute(text('SELECT count(*) FROM '+name)).scalar_one() for name in TABLES}


def api_data(client, route):
    response = client.get('/api/v1/'+route)
    assert response.status_code == 200, 'read API acceptance failed'
    return response.json()['data']


def test_history_transaction_lineage_idempotence_permissions_and_worker(environment):
    engine, users = environment
    assert engine.url.database == 'rp1_presence_history_pc3', 'disposable history database required'
    path = Path(os.environ['HISTORY_CSV'])
    rows = load_csv(path)
    with engine.connect() as conn:
        actor = str(conn.execute(text('SELECT public_id FROM iam.app_user WHERE username=:u'), {'u':users['admin']}).scalar_one())
    with engine.begin() as conn:
        set_context(conn, actor)
        summary = import_execution_history(conn, path)
    assert all(summary[k] == v for k,v in {'rows':135,'assets':7,'campaigns':4,'stations':3,'executions':135,'results':135,'lineage':135,'runtimeIntervals':124}.items()), 'import counts mismatch'
    with engine.connect() as conn:
        lineage = conn.execute(text('SELECT source_execution_key,row_hash,raw_payload,normalization_warnings FROM integration.execution_import_record')).mappings().all()
        assert len(lineage) == 135
        by_key = {r['source_execution_key']:r for r in lineage}
        for row in rows:
            saved = by_key[row['test_id']]
            identical = saved['row_hash'] == row['row_hash'] and saved['raw_payload'] == row['raw_payload'] and saved['normalization_warnings'] == row['normalization_warnings']
            assert identical, 'source lineage or quality flags changed'
        warnings = Counter(w['code'] for r in lineage for w in r['normalization_warnings'])
        assert warnings['reported_duration_mismatch'] == 6 and warnings['source_status_semantic_conflict'] == 49
        assert conn.execute(text("SELECT count(*) FROM test.runtime_interval WHERE source_kind='LEGACY_REPORTED'")).scalar_one() == 124
        assert conn.execute(text('SELECT count(*) FROM reliability.exposure_assessment')).scalar_one() == 0
        assert conn.execute(text('SELECT count(*) FROM reliability.current_mtbf_result')).scalar_one() == 0
        assert conn.execute(text('SELECT count(*) FROM integration.hmi_presence')).scalar_one() == 0
        assert conn.execute(text("SELECT count(*) FROM test.asset WHERE asset_code ILIKE '%SHOWCASE%'")).scalar_one() == 0
        before = counts(conn)
        execution_ids = set(conn.execute(text('SELECT id,public_id FROM test.test_execution')).all())
        runtime_ids = set(conn.execute(text('SELECT id,public_id FROM test.runtime_interval')).all())
    with engine.begin() as conn:
        set_context(conn, actor)
        import_execution_history(conn, path)
    with engine.connect() as conn:
        after = counts(conn)
        assert all(before[t] == after[t] for t in TABLES if t != 'audit.change_log'), 'idempotent counts changed'
        assert execution_ids == set(conn.execute(text('SELECT id,public_id FROM test.test_execution')).all())
        assert runtime_ids == set(conn.execute(text('SELECT id,public_id FROM test.runtime_interval')).all())
        audit_before = after['audit.change_log']
    # A conflicting user asset must roll back all preceding importer writes.
    with pytest.raises(ValueError, match='already used by non-legacy data'):
        with engine.begin() as conn:
            set_context(conn, actor)
            conn.execute(text("UPDATE test.asset SET product_family='QA-COLLISION' WHERE asset_code='RP1.3-SARM-001'"))
            import_execution_history(conn, path)
    with engine.connect() as conn:
        assert counts(conn)['audit.change_log'] == audit_before
        assert conn.execute(text("SELECT count(*) FROM test.asset WHERE product_family='QA-COLLISION'")).scalar_one() == 0
        assert conn.execute(text('SELECT count(*) FROM test.test_execution')).scalar_one() == 135
        missing_refs = conn.execute(text('''SELECT count(*) FROM test.test_execution e
          LEFT JOIN test.asset a ON a.id=e.asset_id LEFT JOIN test.test_campaign c ON c.id=e.campaign_id
          LEFT JOIN test.configuration_snapshot s ON s.id=e.configuration_snapshot_id
          LEFT JOIN test.test_cycle cy ON cy.id=e.cycle_id LEFT JOIN test.station st ON st.id=e.station_id
          LEFT JOIN catalog.test_case_version v ON v.id=e.test_case_version_id
          WHERE a.id IS NULL OR c.id IS NULL OR s.id IS NULL OR cy.id IS NULL OR st.id IS NULL OR v.id IS NULL''')).scalar_one()
        assert missing_refs == 0
        campaign = conn.execute(text("SELECT id,public_id FROM test.test_campaign WHERE status='ACTIVE' ORDER BY id LIMIT 1")).one()
        viewer = str(conn.execute(text('SELECT public_id FROM iam.app_user WHERE username=:u'), {'u':users['viewer']}).scalar_one())
    with signed_in(users['admin']) as admin, signed_in(users['viewer']) as viewer_client:
        assets = api_data(admin,'assets?limit=100')['items']
        assert len(assets) == 7
        assert all(a['durations']['active_elapsed_seconds'] == 0 and a['current_execution_id'] is None for a in assets)
        home = api_data(admin,'dashboard/home')
        scopes = [s for group in home['groups'] for s in group['objects']]
        assert all(s['active_execution_count'] == 0 and s['part_durations']['active_elapsed_seconds'] == 0 for s in scopes)
        home_cases = [case for scope in scopes for case in scope['test_cases']]
        assert sum(scope['active_execution_count'] for scope in scopes) == 0
        assert sum(scope['part_durations']['effective_exposure_seconds'] for scope in scopes) == 0
        assert sum(scope['part_durations']['excluded_seconds'] for scope in scopes) == 0
        assert sum(scope['part_durations']['pending_seconds'] for scope in scopes) == sum(scope['part_durations']['total_duration_seconds'] for scope in scopes)
        assert sum(case['execution_count'] for case in home_cases) == 135
        assert sum(case['historical_execution_count'] for case in home_cases) == 135
        assert sum(case['platform_execution_count'] for case in home_cases) == 0
        assert sum(case['passed_execution_count'] for case in home_cases) == 40
        assert sum(case['failed_execution_count'] for case in home_cases) == 17
        assert sum(case['inconclusive_execution_count'] for case in home_cases) == 67
        assert sum(case['not_evaluated_execution_count'] for case in home_cases) == 11
        assert sum(case['exception_count'] for case in home_cases) == 16
        recorded_cases = [case for case in home_cases if case['execution_count']]
        assert all(case['first_recorded_at'] and case['latest_recorded_at'] for case in recorded_cases)
        assert {station for case in recorded_cases for station in case['station_codes']} == {'RD-01','RD-02','RD-03'}
        ledger = api_data(admin,'test-cases/durations?limit=100')
        assert ledger['totals']['active_elapsed_seconds'] == 0
        all_campaigns = api_data(admin,'setup/campaigns')
        assert len(all_campaigns) == 4
        assert len(api_data(viewer_client,'setup/campaigns')) == 4
        assert len(api_data(viewer_client,'assets?limit=100')['items']) == 7
        for other in all_campaigns:
            if other['id'] != str(campaign.public_id):
                revoked = admin.request('DELETE', '/api/v1/admin/users/'+viewer+'/campaign-grants/'+other['id'], json={'reason':REASON})
                assert revoked.status_code == 204, 'isolated default VIEW revoke failed'
        total = 0
        for asset in assets:
            asset_detail = api_data(admin,'assets/'+asset['id'])
            assert asset_detail['current_context']['execution_id'] is None
            route = 'assets/'+asset['id']+'/executions?limit=100'
            page = api_data(admin,route)
            assert all(e['active_elapsed_seconds'] == 0 for e in page['items'])
            total += len(page['items'])
            while page.get('next_cursor'):
                page = api_data(admin,route+'&cursor='+page['next_cursor'])
                assert all(e['active_elapsed_seconds'] == 0 for e in page['items'])
                total += len(page['items'])
        assert total == 135
        assert len(api_data(viewer_client,'setup/campaigns')) == 4
        assert len(api_data(viewer_client,'assets?limit=100')['items']) == 7
        with engine.connect() as conn:
            warned_execution = str(conn.execute(text('SELECT e.public_id FROM test.test_execution e JOIN test.execution_result r ON r.execution_id=e.id WHERE jsonb_array_length(r.normalization_flags)>0 ORDER BY e.id LIMIT 1')).scalar_one())
        detail = api_data(admin,'executions/'+warned_execution)
        assert detail['result'] and detail['result']['normalization_flags']
        assert detail['execution']['active_elapsed_seconds'] == 0
        case = api_data(admin,'test-cases/REL-UPPER-001')
        assert case['durations']['active_elapsed_seconds'] == 0
    # A RUNNING row without a fresh workstation heartbeat is not a live execution.
    with engine.connect() as conn:
        transaction = conn.begin()
        session = Session(bind=conn)
        try:
            set_context(session, actor)
            native = session.execute(text('''INSERT INTO test.test_execution(
                execution_code,campaign_id,cycle_id,asset_id,configuration_snapshot_id,
                test_case_version_id,station_id,status,received_at,normalized_started_at)
              SELECT 'QA-NATIVE-LIVE-HISTORY-REGRESSION',campaign_id,cycle_id,asset_id,
                configuration_snapshot_id,test_case_version_id,station_id,'RUNNING',
                clock_timestamp(),clock_timestamp()-interval '2 minutes'
              FROM test.test_execution WHERE status='RUNNING' ORDER BY id LIMIT 1
              RETURNING public_id,asset_id''')).one()
            repo = ReadModelRepository(session)
            native_detail = repo.get_execution(str(native.public_id))
            assert native_detail.execution.active_elapsed_seconds == 0
            asset_id = str(session.execute(text('SELECT public_id FROM test.asset WHERE id=:id'),{'id':native.asset_id}).scalar_one())
            asset_detail = repo.get_asset(asset_id)
            assert asset_detail.current_context.execution_id is None
            assert asset_detail.asset.durations.active_elapsed_seconds == 0
            home = repo.dashboard_home()
            scopes = [s for group in home.groups for s in group.objects]
            assert sum(s.active_execution_count for s in scopes) == 0
            assert sum(s.part_durations.active_elapsed_seconds for s in scopes) == 0
            ledger = repo.test_case_durations(asset_kind=None,asset=None,test_case=None,status=None,search=None,limit=100,cursor=None)
            assert ledger.totals.active_elapsed_seconds == 0
            case = repo.get_test_case(native_detail.execution.test_case_code,limit=100,cursor=None)
            assert case.durations.active_elapsed_seconds == 0
        finally:
            session.close()
            transaction.rollback()
    # Exercise actual Worker computation inside a rolled-back isolated transaction.
    with engine.connect() as conn:
        transaction = conn.begin()
        session = Session(bind=conn)
        try:
            set_context(session, actor)
            scope = session.execute(text("SELECT id FROM reliability.mtbf_scope WHERE scope_code='OVERALL' AND asset_kind='MODULE' AND status='PUBLISHED' ORDER BY version DESC LIMIT 1")).scalar_one()
            campaigns = session.execute(text('SELECT id,public_id FROM test.test_campaign ORDER BY id')).all()
            for campaign in campaigns:
                now = datetime.now(timezone.utc)
                intervals = _interval_rows(session,campaign.id,scope,now,now)
                assert intervals and all(r['source_kind']=='LEGACY_REPORTED' and r['disposition']=='PENDING' for r in intervals)
                MtbfRepository(session).upsert_config(campaign.public_id,'OVERALL',CampaignMtbfConfigRequest(target_hours=1000,confidence_levels=[.7,.9],settings={'source':'isolated history acceptance'}),None)
            for _ in campaigns:
                job = claim_job(session,'pc3-isolated-history-worker')
                assert job is not None
                process_job(session,job)
            projections = session.execute(text('SELECT exposure_seconds,point_estimate_hours,lower_70_hours,result_payload FROM reliability.current_mtbf_result')).mappings().all()
            assert len(projections) == 4
            assert all(float(p['exposure_seconds']) == 0 and p['point_estimate_hours'] is None and float(p['lower_70_hours']) == 0 and p['result_payload']['point_estimate_status'] == 'NO_EXPOSURE' for p in projections), 'legacy data entered formal MTBF'
            assert all(p['result_payload']['total_test_duration_hours'] > 0 and p['result_payload']['eligible_exposure_hours'] == 0 for p in projections)
            confirmation = (Path(__file__).resolve().parents[2] / 'database/sql/027_confirm_legacy_reported_exposure.sql').read_text()
            session.connection().exec_driver_sql(confirmation)
            confirmed_jobs = 0
            while True:
                job = claim_job(session, 'pc3-isolated-history-worker')
                if job is None:
                    break
                process_job(session, job)
                confirmed_jobs += 1
            assert confirmed_jobs > 0
            expected_seconds = session.execute(text("SELECT coalesce(sum(active_seconds),0) FROM test.runtime_interval WHERE source_kind='LEGACY_REPORTED' AND NOT voided")).scalar_one()
            included_seconds = session.execute(text('SELECT coalesce(sum(exposure_seconds),0) FROM reliability.current_mtbf_result')).scalar_one()
            assert included_seconds == expected_seconds
            confirmed_home = ReadModelRepository(session).dashboard_home()
            confirmed_scopes = [item for group in confirmed_home.groups for item in group.objects]
            assert sum(item.active_execution_count for item in confirmed_scopes) == 0
            assert abs(sum(item.part_durations.effective_exposure_seconds for item in confirmed_scopes) - float(expected_seconds)) < 0.001
            assert sum(item.part_durations.excluded_seconds for item in confirmed_scopes) == 0
            assert sum(item.part_durations.pending_seconds for item in confirmed_scopes) == 0
        finally:
            session.close()
            transaction.rollback()
    with engine.connect() as conn:
        assert conn.execute(text('SELECT count(*) FROM reliability.current_mtbf_result')).scalar_one() == 0
        assert conn.execute(text("SELECT count(*) FROM audit.change_log WHERE change_reason=:r AND actor_user_id IS NOT NULL AND request_id<>''"), {'r':REASON}).scalar_one() > 135
