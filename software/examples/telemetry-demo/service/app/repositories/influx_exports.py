"""Transaction-scoped durable Influx export queue operations."""
from __future__ import annotations

from typing import Any
from sqlalchemy import text
from sqlalchemy.orm import Session


def enqueue_export(session: Session, *, batch_id: int, source_id: int,
                   execution_id: int, lines: str, count: int) -> None:
    session.execute(text("""
        INSERT INTO integration.influx_export_queue(
            batch_id, source_id, execution_id, line_protocol, point_count
        ) VALUES (:batch_id, :source_id, :execution_id, :lines, :count)
        ON CONFLICT (batch_id) DO NOTHING
    """), {"batch_id": batch_id, "source_id": source_id,
           "execution_id": execution_id, "lines": lines, "count": count})


def claim_export(session: Session, worker_id: str) -> dict[str, Any] | None:
    row = session.execute(text('''
        WITH candidate AS (
            SELECT id FROM integration.influx_export_queue
            WHERE (status = 'PENDING' AND available_at <= clock_timestamp())
               OR (status = 'SENDING' AND locked_until < clock_timestamp())
            ORDER BY available_at, id FOR UPDATE SKIP LOCKED LIMIT 1
        )
        UPDATE integration.influx_export_queue queue
        SET status = 'SENDING', attempts = attempts + 1,
            locked_by = :worker_id,
            locked_until = clock_timestamp() + interval '5 minutes'
        FROM candidate WHERE queue.id = candidate.id
        RETURNING queue.id, queue.line_protocol, queue.point_count, queue.attempts
    '''), {'worker_id': worker_id}).mappings().first()
    return dict(row) if row else None


def complete_export(session: Session, job: dict[str, Any], worker_id: str) -> None:
    session.execute(text('''
        UPDATE integration.influx_export_queue
        SET status = 'SENT', sent_at = clock_timestamp(), last_error = '',
            locked_by = NULL, locked_until = NULL
        WHERE id = :id AND locked_by = :worker_id AND status = 'SENDING'
    '''), {'id': job['id'], 'worker_id': worker_id})


def retry_delay(attempts: int) -> int:
    return min(300, 2 ** min(max(attempts, 1), 9))


def fail_export(session: Session, job: dict[str, Any], worker_id: str, error: Exception) -> None:
    session.execute(text('''
        UPDATE integration.influx_export_queue
        SET status = 'PENDING', available_at = clock_timestamp() + make_interval(secs => :delay),
            last_error = :error, locked_by = NULL, locked_until = NULL
        WHERE id = :id AND locked_by = :worker_id AND status = 'SENDING'
    '''), {'id': job['id'], 'worker_id': worker_id, 'delay': retry_delay(job['attempts']),
          'error': str(error)[:500]})
