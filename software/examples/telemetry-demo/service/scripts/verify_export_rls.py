"""Exercise durable export with actual SERVICE/worker database identities.

Run only in the isolated synthetic demo database. All records and key creation
are rolled back, and existing queues are never committed or acknowledged.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import text
from app.core.config import get_settings
from app.core.db import SessionLocal
from app.repositories.influx_exports import claim_export, complete_export, enqueue_export, fail_export
from app.repositories.security import SecurityRepository
from app.services.auth import ServiceCredentialService


def set_actor(session, actor_id) -> None:
    session.execute(text('SELECT iam.set_request_context(:actor,:request,:reason)'), {
        'actor': str(actor_id), 'request': 'verify-export-rls', 'reason': 'rollback-only isolated queue validation',
    })


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--context', type=Path, default=Path('/runtime/context.json'))
    args = parser.parse_args()
    context = json.loads(args.context.read_text(encoding='utf-8'))
    assert context['source_kind'] == 'synthetic', 'verification requires the synthetic demo context'
    with SessionLocal() as session:
        try:
            credentials = ServiceCredentialService(get_settings(), SecurityRepository(session))
            actor = credentials.resolve(context['api_key'])
            assert actor is not None
            set_actor(session, actor.id)
            source_id = session.execute(text('SELECT iam.current_ingestion_source_id()')).scalar_one()
            execution_id = session.execute(text('''
                SELECT id FROM test.test_execution
                WHERE source_id = iam.current_ingestion_source_id()
                  AND telemetry_source_kind = 'synthetic'
                ORDER BY id DESC LIMIT 1
            ''')).scalar_one()
            batch_id = session.execute(text('''
                INSERT INTO integration.ingestion_batch(
                    source_id, producer_batch_key, execution_id, batch_kind, payload_hash, item_count, status
                ) VALUES (:source, :producer, :execution, 'TELEMETRY', :digest, 1, 'RECEIVING') RETURNING id
            '''), {'source': source_id, 'producer': 'rls-test-' + str(uuid4()),
                   'execution': execution_id, 'digest': '0' * 64}).scalar_one()
            values = dict(batch_id=batch_id, source_id=source_id, execution_id=execution_id,
                          lines='joint_telemetry,source_kind=synthetic temp_c=20.0 1', count=1)
            enqueue_export(session, **values)
            enqueue_export(session, **values)
            queue_id = session.execute(text('SELECT id FROM integration.influx_export_queue WHERE batch_id=:batch'), {'batch': batch_id}).scalar_one()
            assert session.execute(text("UPDATE integration.influx_export_queue SET last_error='forbidden' WHERE id=:id"), {'id': queue_id}).rowcount == 0

            set_actor(session, '00000000-0000-7000-8000-000000000002')
            suffix = uuid4().hex[:12]
            other = credentials.provision(
                username='rls-check-' + suffix, display_name='回滚测试采集器',
                source_code='RLS-CHECK-' + suffix.upper(), campaign_ids=[UUID(context['campaign_id'])],
                expires_at=None, request_id='verify-rls', source_ip=None,
            )
            set_actor(session, other.service_id)
            assert session.execute(text('SELECT count(*) FROM integration.influx_export_queue WHERE id=:id'), {'id': queue_id}).scalar_one() == 0
            nested = session.begin_nested()
            try:
                enqueue_export(session, **values)
            except Exception as exc:
                assert getattr(getattr(exc, 'orig', None), 'sqlstate', None) == '42501'
            else:
                raise AssertionError('foreign collector was allowed to insert an export')
            finally:
                nested.rollback()

            set_actor(session, get_settings().worker_user_public_id)
            # Queue tests must not claim an existing batch belonging to the live demo.
            active = session.execute(text("SELECT count(*) FROM integration.influx_export_queue WHERE id<>:id AND status<>'SENT'"), {'id': queue_id}).scalar_one()
            assert active == 0, 'pause uploads and wait for the demo export queue to drain before verification'
            job = claim_export(session, 'rls-worker-1')
            assert job['id'] == queue_id and job['attempts'] == 1
            session.execute(text("UPDATE integration.influx_export_queue SET locked_until=clock_timestamp()-interval '1 second' WHERE id=:id"), {'id': queue_id})
            job = claim_export(session, 'rls-worker-2')
            assert job['id'] == queue_id and job['attempts'] == 2
            fail_export(session, job, 'rls-worker-2', RuntimeError('controlled test failure'))
            status = session.execute(text('SELECT status, locked_by, last_error FROM integration.influx_export_queue WHERE id=:id'), {'id': queue_id}).one()
            assert status == ('PENDING', None, 'controlled test failure')
            session.execute(text("UPDATE integration.influx_export_queue SET available_at=clock_timestamp()-interval '1 second' WHERE id=:id"), {'id': queue_id})
            job = claim_export(session, 'rls-worker-3')
            assert job['attempts'] == 3
            complete_export(session, job, 'wrong-worker')
            assert session.execute(text('SELECT status FROM integration.influx_export_queue WHERE id=:id'), {'id': queue_id}).scalar_one() == 'SENDING'
            complete_export(session, job, 'rls-worker-3')
            assert session.execute(text('SELECT status FROM integration.influx_export_queue WHERE id=:id'), {'id': queue_id}).scalar_one() == 'SENT'
            print('SERVICE enqueue/replay, foreign denial, worker lease recovery/retry/ack verified; all writes rolled back.')
        finally:
            session.rollback()


if __name__ == '__main__':
    main()
