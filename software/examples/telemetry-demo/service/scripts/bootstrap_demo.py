"""Initialize an explicit synthetic context and emit one local collector credential.

Requires the isolated demo DB owner, not the application's database login.
The generated file is secret, must stay outside Git, and replaces prior demo keys.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from uuid import UUID

from sqlalchemy import text
from app.core.config import get_settings
from app.core.db import SessionLocal
from app.repositories.security import SecurityRepository
from app.services.auth import ServiceCredentialService

CONTEXT_SQL = '''
SELECT campaign.public_id AS campaign_id, cycle.public_id AS cycle_id,
       segment.public_id AS segment_id, asset.public_id AS asset_id,
       configuration.public_id AS configuration_id,
       version.public_id AS test_case_version_id, station.public_id AS station_id
FROM test.test_campaign campaign
JOIN test.test_program program ON program.id = campaign.program_id
JOIN test.test_cycle cycle ON cycle.campaign_id = campaign.id
JOIN test.asset asset ON asset.id = cycle.asset_id
JOIN test.configuration_snapshot configuration ON configuration.asset_id = asset.id
JOIN test.analysis_segment segment ON segment.cycle_id = cycle.id
    AND segment.configuration_snapshot_id = configuration.id
CROSS JOIN test.station station
JOIN catalog.test_case test_case ON test_case.case_code = 'SYS-REL-001'
JOIN catalog.test_case_version version ON version.test_case_id = test_case.id
WHERE program.program_code = 'DEMO-SYNTHETIC'
  AND campaign.campaign_code = 'DEMO-SYNTHETIC'
  AND cycle.cycle_code = 'DEMO-SYNTHETIC-001'
  AND configuration.fingerprint = 'demo-synthetic-v1'
  AND station.station_code = 'DEMO-SYNTHETIC'
  AND version.status = 'PUBLISHED'
ORDER BY version.published_at DESC NULLS LAST, version.id DESC LIMIT 1
'''


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=Path('/runtime/context.json'))
    parser.add_argument('--seed-sql', type=Path, default=Path('/seeds/demo_context.sql'))
    args = parser.parse_args()
    with SessionLocal() as session, session.begin():
        session.execute(text(args.seed_sql.read_text(encoding='utf-8')))
        # Seed sets a transaction-local administrator context before provisioning.
        row = session.execute(text(CONTEXT_SQL)).mappings().one()
        service = ServiceCredentialService(get_settings(), SecurityRepository(session))
        existing = session.execute(text("SELECT public_id FROM iam.app_user WHERE username = 'demo-synthetic-collector'" )).scalar_one_or_none()
        if existing is None:
            grant = service.provision(
                username='demo-synthetic-collector', display_name='合成演示采集器',
                source_code='DEMO-SYNTHETIC-COLLECTOR',
                campaign_ids=[UUID(str(row['campaign_id']))], expires_at=None,
                request_id='bootstrap-demo', source_ip=None,
            )
        else:
            grant = service.rotate(
                service_id=UUID(str(existing)), revoke_previous=True, expires_at=None,
                reason='explicit local demo credential rotation', request_id='bootstrap-demo', source_ip=None,
            )
        context = {key: str(value) for key, value in row.items()}
        context.update(api_key=grant.api_key, source_kind='synthetic',
                       case_id='SYS-REL-001', stage_code='DEMO-SYNTHETIC',
                       stage_name='合成软件链路演示')
        args.output.parent.mkdir(parents=True, exist_ok=True)
        # Restrict permissions before any bytes reach disk.
        descriptor = os.open(args.output, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, 'w', encoding='utf-8') as output:
            json.dump(context, output, ensure_ascii=False, indent=2)
            output.write('\n')
    print(f'Synthetic collector context written to {args.output}; credential not printed.')


if __name__ == '__main__':
    main()
