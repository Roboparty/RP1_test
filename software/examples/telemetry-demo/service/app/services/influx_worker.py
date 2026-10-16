"""Lease a durable batch, export, then acknowledge. All errors retain work."""
from __future__ import annotations

import logging
import socket
import time
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.db import SessionLocal
from app.integrations.influxdb import write_lines
from app.repositories.influx_exports import claim_export, complete_export, fail_export

logger = logging.getLogger('rp1_test.influx')


def set_context(session: Session, request_id: str) -> None:
    session.execute(text('SELECT iam.set_request_context(:user_id, :request_id, :reason)'), {
        'user_id': get_settings().worker_user_public_id,
        'request_id': request_id, 'reason': 'durable Influx projection',
    })


def run_worker() -> None:
    settings = get_settings()
    if not settings.influx_enabled:
        raise RuntimeError('Set INFLUX_ENABLED=true before starting the export worker')
    worker_id = f'{socket.gethostname()}:{uuid4()}'
    while True:
        job = None
        try:
            with SessionLocal() as session, session.begin():
                set_context(session, f'{worker_id}:claim')
                job = claim_export(session, worker_id)
            if job is not None:
                try:
                    write_lines(settings, job['line_protocol'])
                except Exception as exc:
                    with SessionLocal() as session, session.begin():
                        set_context(session, f'{worker_id}:retry:{job["id"]}')
                        fail_export(session, job, worker_id, exc)
                    logger.warning('Export %s retained for retry (%s)', job['id'], type(exc).__name__)
                else:
                    with SessionLocal() as session, session.begin():
                        set_context(session, f'{worker_id}:ack:{job["id"]}')
                        complete_export(session, job, worker_id)
        except Exception as exc:
            # A DB failure leaves a pending item or a recoverable lease.
            logger.error('Export loop failed (%s)', type(exc).__name__)
            time.sleep(settings.worker_poll_seconds)
        if job is None:
            time.sleep(settings.worker_poll_seconds)


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    run_worker()
