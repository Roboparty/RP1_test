#!/usr/bin/env python3
"""Read-only PC3 checks; never reads passwords from database or creates sessions."""
import json
from pathlib import Path
import ssl
import subprocess
import urllib.request
from urllib.error import HTTPError

ROOT = Path(__file__).resolve().parents[1]
values = dict(line.split('=', 1) for line in (ROOT / '.env').read_text().splitlines() if line and not line.startswith('#'))
compose = str(ROOT / 'scripts/compose.sh')
actor = subprocess.check_output([compose, 'exec', '-T', 'postgres', 'psql', '-U', values['POSTGRES_USER'], '-d', values['POSTGRES_DB'], '-Atc', "SELECT public_id FROM iam.app_user WHERE username='admin' AND enabled AND role='SYSTEM_ADMIN'"], text=True).strip()
assert actor and '\n' not in actor, 'one enabled administrator required'
code = '''
import json, sys
from sqlalchemy import text
from app.core.db import SessionLocal
from app.repositories.read_models import ReadModelRepository
from app.repositories.setup import SetupRepository
with SessionLocal() as session, session.begin():
    session.execute(text('SET TRANSACTION READ ONLY'))
    session.execute(text("SELECT iam.set_request_context(:actor,'pc3-read-only-verify','deployment verification')"), {'actor':sys.argv[1]})
    home=ReadModelRepository(session).dashboard_home()
    setup=SetupRepository(session)
    scopes=[s for g in home.groups for s in g.objects]
    assert len(scopes)==8
    assert sum(len(s.test_cases) for s in scopes)==67
    assert len(setup.catalog())==68
    print(json.dumps({'catalog':len(setup.catalog()),'scopes':[{'part':s.target_part_code,'samples':s.part_asset_count,'cases':len(s.test_cases)} for s in scopes],'plans':len(setup.campaigns())},ensure_ascii=False))
'''
subprocess.run([compose, 'exec', '-T', 'api', 'python', '-', actor], input=code, text=True, check=True)
tls = ssl.create_default_context(cafile=str(ROOT / 'deployment/secrets/root-ca.crt'))
base = values['PLATFORM_URL']
posters = ['assets/humanoid-robot.webp', *('model-posters/' + name + '.webp' for name in ('upper-product-white','lower-product-white','torso-product-white','head-product-white','bat','single-arm-product-white','single-leg-product-white'))]
for poster in posters:
    with urllib.request.urlopen(base + '/' + poster, context=tls, timeout=15) as response:
        content = response.read()
        assert response.status == 200 and response.headers.get_content_type() == 'image/webp'
        assert content[:4] == b'RIFF' and content[8:12] == b'WEBP'
        print('PASS model poster ' + poster)
with urllib.request.urlopen(base + '/openapi.json', context=tls, timeout=15) as response:
    paths = json.load(response)['paths']
    assert '/api/v1/setup/assets' in paths and '/api/v1/setup/campaigns/{campaign_id}/contexts' in paths
for path in ('/api/v1/setup/catalog', '/api/v1/setup/campaigns'):
    try:
        urllib.request.urlopen(base + path, context=tls, timeout=15)
        raise AssertionError('unauthenticated setup request accepted')
    except HTTPError as exc:
        assert exc.code == 401
        print('PASS authentication required ' + path)
