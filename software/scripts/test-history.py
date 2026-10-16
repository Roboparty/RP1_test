#!/usr/bin/env python3
"""Run history acceptance in a disposable DB using existing approved roles."""
import os
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]
values = dict(l.split('=',1) for l in (ROOT/'.env').read_text().splitlines() if l and not l.startswith('#'))
DB = 'rp1_presence_history_pc3'
container = values['COMPOSE_PROJECT_NAME']+'-postgres-1'
network = values['COMPOSE_PROJECT_NAME']+'_default'
env = os.environ.copy()
admin = f"postgresql+psycopg://{values['POSTGRES_USER']}:{values['POSTGRES_PASSWORD']}@postgres:5432/{DB}"
env.update({'DATABASE_URL':admin,'RP1_APP_USER':values['RP1_APP_USER'],'RP1_READONLY_USER':values['RP1_READONLY_USER'],'IMPORT_LEGACY_HISTORY':'false','HMI_TEST_ADMIN_URL':admin,'APP_ENV':'test','AUTH_MODE':'session','AUTH_COOKIE_SECURE':'false','PYTHONPATH':'/app','SEED_ADMIN_USERNAME':'','HISTORY_CSV':'/incoming/executions_202609181314.csv'})
def run(args): subprocess.run(args,env=env,check=True)
def flags(names): return sum((['-e',name] for name in names),[])
os.umask(0o077)
run(['docker','exec',container,'createdb','-U',values['POSTGRES_USER'],DB])
try:
    run(['docker','run','--rm','--network',network,'-v',str(ROOT/'database')+':/workspace:ro',*flags(('DATABASE_URL','RP1_APP_USER','RP1_READONLY_USER','IMPORT_LEGACY_HISTORY')),'rp1-test-platform-migrate:1.0.1'])
    env['DATABASE_URL']=f"postgresql+psycopg://{values['RP1_APP_USER']}:{values['RP1_APP_PASSWORD']}@postgres:5432/{DB}"
    run(['docker','run','--rm','--user',str(os.getuid()),'--network',network,'-v',str(ROOT/'backend')+':/app:ro','-v',str(ROOT/'database')+':/database:ro','-v',str(ROOT/'.qa/history-incoming')+':/incoming:ro',*flags(('DATABASE_URL','HMI_TEST_ADMIN_URL','APP_ENV','AUTH_MODE','AUTH_COOKIE_SECURE','PYTHONPATH','SEED_ADMIN_USERNAME','HISTORY_CSV')),'rp1-pc3-qa-tools:local','python','-m','pytest','-p','no:cacheprovider','-c','pytest.ini','tests/test_history_migration_integration.py','-q','--tb=short'])
finally:
    run(['docker','exec',container,'dropdb','-U',values['POSTGRES_USER'],DB])
