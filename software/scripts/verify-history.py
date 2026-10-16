#!/usr/bin/env python3
"""Read-only production history acceptance; no login sessions or credentials created."""
import inspect
import json
import os
from pathlib import Path
import ssl
import subprocess
import urllib.request
from urllib.error import HTTPError


def verify_inside(actor):
    import json
    from collections import Counter
    from sqlalchemy import text
    from app.core.db import SessionLocal
    from app.repositories.read_models import ReadModelRepository
    from app.repositories.setup import SetupRepository
    with SessionLocal() as session, session.begin():
        session.execute(text('SET TRANSACTION READ ONLY'))
        session.execute(text("SELECT iam.set_request_context(:actor,'pc3-history-read-only-verify','authorized deployment verification')"), {'actor':actor})
        repo = ReadModelRepository(session)
        setup = SetupRepository(session)
        home = repo.dashboard_home()
        scopes = [s for g in home.groups for s in g.objects]
        assert len(scopes) == 8 and sum(len(s.test_cases) for s in scopes) == 68
        assert len(setup.catalog()) == 69
        plans = setup.campaigns()
        assert len(plans) == 4
        assets = repo.list_assets(asset_kind=None,target_part_code=None,status=None,search=None,limit=100,cursor=None)
        assert len(assets.items) == 7 and assets.page.next_cursor is None
        executions = []
        for asset in assets.items:
            detail = repo.get_asset(str(asset.id))
            assert detail.asset.durations.effective_exposure_seconds == 0
            assert detail.asset.durations.active_elapsed_seconds == 0
            page = repo.asset_executions(str(asset.id),100,None)
            executions.extend(page.items)
            while page.page.next_cursor:
                page = repo.asset_executions(str(asset.id),100,page.page.next_cursor)
                executions.extend(page.items)
            trends = repo.asset_performance_trends(str(asset.id))
            assert all(not series.points for metric in trends.metrics for series in metric.series), 'unexpected telemetry curve'
        assert len(executions) == 135
        flags = Counter()
        source_statuses = Counter()
        for execution in executions:
            detail = repo.get_execution(str(execution.id))
            assert detail.result is not None
            source_statuses[detail.result.source_status] += 1
            flags.update(flag['code'] for flag in detail.result.normalization_flags)
        assert flags['reported_duration_mismatch'] == 6 and flags['source_status_semantic_conflict'] == 49
        ledger = repo.test_case_durations(asset_kind=None,asset=None,test_case=None,status=None,search=None,limit=100,cursor=None)
        assert ledger.page.next_cursor is None and sum(i.execution_count for i in ledger.items) == 135
        assert ledger.totals.effective_exposure_seconds == 0 and ledger.totals.active_elapsed_seconds == 0
        sql_total = float(session.execute(text("SELECT sum(active_seconds) FROM test.runtime_interval WHERE source_kind='LEGACY_REPORTED'")).scalar_one())
        assert abs(ledger.totals.total_duration_seconds - sql_total) < 1e-6
        assert abs(sum(s.part_durations.total_duration_seconds for s in scopes) - sql_total) < 1e-6
        assert all(s.part_durations.effective_exposure_seconds == 0 for s in scopes)
        assert sum(s.part_asset_count for s in scopes) == 7
        assert all(s.active_execution_count == 0 for s in scopes)
        counts = {table:session.execute(text('SELECT count(*) FROM '+table)).scalar_one() for table in (
            'test.station','test.runtime_interval','integration.execution_import_record',
            'reliability.exposure_assessment','reliability.campaign_mtbf_config',
            'reliability.current_mtbf_result','integration.hmi_presence','health.metric_series')}
        assert counts['test.station'] == 3 and counts['test.runtime_interval'] == 124
        assert counts['integration.execution_import_record'] == 135
        assert all(counts[t] == 0 for t in ('reliability.exposure_assessment','reliability.campaign_mtbf_config','reliability.current_mtbf_result','integration.hmi_presence','health.metric_series'))
        assert session.execute(text("SELECT count(*) FROM test.runtime_interval WHERE source_kind <> 'LEGACY_REPORTED' OR ended_at IS NULL")).scalar_one() == 0
        assert session.execute(text("SELECT count(*) FROM integration.import_batch WHERE batch_code='EXECUTION-CSV-20260918' AND status='SUCCEEDED'")).scalar_one() == 1
        report = {'verification':'PASS','read_only':True,'catalog':69,'enabled_published_cases':68,
            'samples':7,'plans':4,'stations':3,'executions':135,'legacy_reported_intervals':124,
            'historical_cumulative_seconds':sql_total,'effective_exposure_seconds':0,'live_elapsed_seconds':0,
            'formal_mtbf_results':0,'telemetry_series':0,'source_statuses':dict(source_statuses),
            'warnings':dict(flags),'scopes':[{'part':s.target_part_code,'samples':s.part_asset_count,
                'cases':len(s.test_cases),'historical_seconds':s.part_durations.total_duration_seconds,
                'effective_seconds':s.part_durations.effective_exposure_seconds} for s in scopes]}
        print(json.dumps({'report':report,'home':home.model_dump(mode='json')},sort_keys=True))


def main():
    root = Path(__file__).resolve().parents[1]
    settings = dict(line.split('=',1) for line in (root/'.env').read_text().splitlines() if line and not line.startswith('#'))
    compose = str(root/'scripts/compose.sh')
    actor = subprocess.check_output([compose,'exec','-T','postgres','psql','-U',settings['POSTGRES_USER'],'-d',settings['POSTGRES_DB'],'-Atc',"SELECT public_id FROM iam.app_user WHERE username='admin' AND enabled AND role='SYSTEM_ADMIN'"],text=True).strip()
    assert actor and '\n' not in actor, 'one enabled administrator required'
    result = subprocess.run([compose,'exec','-T','api','python','-',actor],input=inspect.getsource(verify_inside)+'\nimport sys\nverify_inside(sys.argv[1])\n',text=True,capture_output=True)
    os.umask(0o077)
    if result.returncode:
        (root/'.qa/pc3-setup/history-read-only-error.log').write_text(result.stderr)
        raise SystemExit('Read-only history verification failed; diagnostic saved locally.')
    payload = json.loads(result.stdout)
    context = ssl.create_default_context(cafile=str(root/'deployment/secrets/root-ca.crt'))
    base = settings['PLATFORM_URL']
    for route in ('/healthz','/readyz','/','/openapi.json'):
        with urllib.request.urlopen(base+route,context=context,timeout=15) as response:
            assert response.status == 200
    for route in ('/api/v1/dashboard/home','/api/v1/assets','/api/v1/setup/campaigns','/api/v1/test-cases/durations'):
        try:
            urllib.request.urlopen(base+route,context=context,timeout=15)
            raise AssertionError('anonymous read unexpectedly accepted')
        except HTTPError as exc:
            assert exc.code == 401
    payload['report']['strict_https_and_anonymous_401'] = 'PASS'
    (root/'.qa/pc3-setup/history-production-verification.json').write_text(json.dumps(payload['report'],indent=2,sort_keys=True)+'\n')
    preview = root/'frontend/node_modules/.cache/rp1-layout'
    assert preview.is_dir()
    (preview/'home.json').write_text(json.dumps(payload['home'])+'\n')
    print(json.dumps(payload['report'],sort_keys=True))


if __name__ == '__main__':
    main()
