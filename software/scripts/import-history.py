#!/usr/bin/env python3
"""One authorized PC3 import; fail closed if the backed-up empty target changed."""
import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

SHA256 = 'd4b4c6e96ed9fb9e05267d77cea0f747931ea72458f30994e538e24f1cd68c0c'
EXPECTED = {'rows':135, 'assets':7, 'campaigns':4, 'stations':3,
            'executions':135, 'results':135, 'lineage':135, 'runtimeIntervals':124}


def inside():
    from sqlalchemy import create_engine, text
    sys.path.insert(0, '/database')
    from imports.execution_csv import import_execution_history, load_csv
    path = Path('/incoming/executions_202609181314.csv')
    assert hashlib.sha256(path.read_bytes()).hexdigest() == SHA256, 'source digest changed'
    rows = load_csv(path)
    engine = create_engine(os.environ['DATABASE_URL'], hide_parameters=True)
    assert engine.url.database == os.environ['EXPECTED_TARGET_DB'], 'wrong target database'
    request = 'pc3-history-import-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    with engine.begin() as conn:
        conn.execute(text("SET LOCAL lock_timeout='10s'"))
        conn.execute(text("SET LOCAL statement_timeout='120s'"))
        tables = ('test.asset', 'test.test_campaign', 'test.station', 'test.test_execution',
                  'test.runtime_interval', 'integration.execution_import_record',
                  'integration.import_batch', 'integration.ingestion_source', 'test.site',
                  'test.lab', 'test.manufacturing_batch', 'test.test_program',
                  'catalog.test_case', 'catalog.test_case_version')
        conn.execute(text('LOCK TABLE '+','.join(tables)+' IN SHARE ROW EXCLUSIVE MODE'))
        assert conn.execute(text('SELECT version_num FROM alembic_version')).scalar_one() == '20261008_0024', 'unexpected schema'
        for table in tables[:7]:
            assert conn.execute(text('SELECT count(*) FROM '+table)).scalar_one() == 0, 'target changed; no overwrite permitted'
        reserved = (
            ('integration.ingestion_source','source_code','LEGACY-EXECUTION-CSV-20260918'),
            ('test.site','site_code','LEGACY-EXEC-20260918'),
            ('test.manufacturing_batch','batch_code','LEGACY-EXEC-20260918-MODULES'),
            ('test.test_program','program_code','LEGACY-EXECUTION-HISTORY-20260918'),
        )
        for table,column,value in reserved:
            assert conn.execute(text('SELECT count(*) FROM '+table+' WHERE '+column+'=:v'), {'v':value}).scalar_one() == 0, 'reserved identity already exists'
        assert conn.execute(text('SELECT count(*) FROM reliability.campaign_mtbf_config')).scalar_one() == 0, 'unexpected MTBF configuration'
        principal_before = conn.execute(text('SELECT count(*) FROM iam.app_user')).scalar_one()
        actor = str(conn.execute(text("SELECT public_id FROM iam.app_user WHERE username='admin' AND enabled AND role='SYSTEM_ADMIN'")).scalar_one())
        reason = 'authorized non-SHOWCASE execution history migration'
        conn.execute(text('SELECT iam.set_request_context(:actor,:request,:reason)'), {'actor':actor,'request':request,'reason':reason})
        summary = import_execution_history(conn,path)
        assert all(summary[k] == v for k,v in EXPECTED.items()), 'import counts mismatch'
        lineage = conn.execute(text('SELECT source_execution_key,row_hash,raw_payload,normalization_warnings FROM integration.execution_import_record')).mappings().all()
        assert len(lineage) == 135, 'lineage count mismatch'
        by_key = {r['source_execution_key']:r for r in lineage}
        for row in rows:
            saved = by_key[row['test_id']]
            assert all(saved[a] == row[b] for a,b in (('row_hash','row_hash'),('raw_payload','raw_payload'),('normalization_warnings','normalization_warnings'))), 'source lineage mismatch'
        flags = Counter(w['code'] for r in lineage for w in r['normalization_warnings'])
        assert flags['reported_duration_mismatch'] == 6 and flags['source_status_semantic_conflict'] == 49, 'source warnings changed'
        assert conn.execute(text("SELECT count(*) FROM test.runtime_interval WHERE source_kind='LEGACY_REPORTED' AND ended_at IS NOT NULL")).scalar_one() == 124, 'legacy runtime mismatch'
        for table in ('reliability.exposure_assessment','reliability.current_mtbf_result','integration.hmi_presence','health.metric_series'):
            assert conn.execute(text('SELECT count(*) FROM '+table)).scalar_one() == 0, 'unexpected formal or live data'
        assert conn.execute(text("SELECT count(*) FROM test.asset WHERE asset_code ILIKE '%SHOWCASE%'")).scalar_one() == 0, 'SHOWCASE detected'
        assert conn.execute(text('SELECT count(*) FROM iam.app_user')).scalar_one() == principal_before, 'principal count changed'
        audit_count = conn.execute(text('SELECT count(*) FROM audit.change_log WHERE request_id=:request AND actor_user_id IS NOT NULL AND change_reason=:reason'),{'request':request,'reason':reason}).scalar_one()
        assert audit_count > 135, 'audited import required'
        statuses = dict(conn.execute(text('SELECT status,count(*) FROM test.test_execution GROUP BY status')).all())
        outcomes = dict(conn.execute(text('SELECT outcome,count(*) FROM test.execution_result GROUP BY outcome')).all())
        seconds = str(conn.execute(text('SELECT sum(active_seconds) FROM test.runtime_interval')).scalar_one())
        report = {'committed':True,'source_sha256':SHA256,'request_id':request,'summary':summary,
                  'source_statuses':dict(Counter(r['source_status'] for r in rows)),
                  'normalized_statuses':statuses,'normalized_outcomes':outcomes,
                  'warnings':dict(flags),'legacy_reported_seconds':seconds,'formal_effective_exposure_seconds':0,
                  'formal_mtbf_results':0,'metric_series':0,'audit_records':audit_count,
                  'principals_unchanged':True,'showcase_records':0}
    engine.dispose()
    print(json.dumps(report,sort_keys=True))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--inside',action='store_true')
    args = parser.parse_args()
    if args.inside:
        try:
            inside()
        except Exception as exc:
            # Never disclose connection credentials, SQL parameters, or source records.
            print(json.dumps({'committed':False,'error_type':type(exc).__name__,
                              'message':'Import failed; check target safely before retrying.'}),file=sys.stderr)
            return 1
        return 0
    root = Path(__file__).resolve().parents[1]
    backup = Path((root/'.qa/pc3-setup/history-backup-path.txt').read_text().strip())
    assert backup.parent == root/'backups' and backup.is_dir(), 'verified local backup required'
    for line in (backup/'SHA256SUMS').read_text().splitlines():
        expected,name = line.split(maxsplit=1)
        assert Path(name).name == name, 'invalid backup manifest'
        assert hashlib.sha256((backup/name).read_bytes()).hexdigest() == expected, 'backup digest mismatch'
    settings = dict(line.split('=',1) for line in (root/'.env').read_text().splitlines() if line and not line.startswith('#'))
    from urllib.parse import quote
    env = os.environ.copy()
    env['DATABASE_URL'] = 'postgresql+psycopg://'+quote(settings['POSTGRES_USER'],safe='')+':'+quote(settings['POSTGRES_PASSWORD'],safe='')+'@postgres:5432/'+quote(settings['POSTGRES_DB'],safe='')
    env['EXPECTED_TARGET_DB'] = settings['POSTGRES_DB']
    result = subprocess.run(['docker','run','--rm','--read-only','--user',str(os.getuid()),
        '--network',settings['COMPOSE_PROJECT_NAME']+'_default','-v',str(root/'database')+':/database:ro',
        '-v',str(root/'.qa/history-incoming')+':/incoming:ro','-v',str(Path(__file__).resolve())+':/import-history.py:ro',
        '-e','DATABASE_URL','-e','EXPECTED_TARGET_DB','rp1-pc3-qa-tools:local','python','/import-history.py','--inside'],
        env=env,text=True,capture_output=True)
    os.umask(0o077)
    if result.returncode:
        print('Production history import blocked; no source records or credentials disclosed.',file=sys.stderr)
        return result.returncode
    report = json.loads(result.stdout)
    assert report['committed'] is True
    report['pre_import_backup'] = str(backup)
    (root/'.qa/pc3-setup/history-production-import.json').write_text(json.dumps(report,indent=2,sort_keys=True)+'\n')
    print(json.dumps(report,sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
