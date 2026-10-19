"""Durable SQLite HTTP outbox with idempotency-key de-duplication."""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class OutboxItem:
    id: int
    idempotency_key: str
    method: str
    path: str
    payload: dict[str, Any]
    attempts: int
    producer_execution_key: str | None
    requires_stage_id: bool
    captures_resolution: bool


class SQLiteOutbox:
    def __init__(self, path: Path) -> None:
        self.path = path.expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS outbox (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    method TEXT NOT NULL,
                    path TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at REAL NOT NULL DEFAULT 0,
                    last_error TEXT,
                    created_at REAL NOT NULL
                )
                """
            )
            columns = {
                str(row["name"]) for row in db.execute("PRAGMA table_info(outbox)").fetchall()
            }
            migrations = {
                "producer_execution_key": "TEXT",
                "requires_stage_id": "INTEGER NOT NULL DEFAULT 0",
                "captures_resolution": "INTEGER NOT NULL DEFAULT 0",
            }
            for name, definition in migrations.items():
                if name not in columns:
                    db.execute(f"ALTER TABLE outbox ADD COLUMN {name} {definition}")
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS execution_resolution (
                    producer_execution_key TEXT PRIMARY KEY,
                    execution_id TEXT NOT NULL,
                    stage_id TEXT NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    def enqueue(
        self,
        method: str,
        path: str,
        payload: dict[str, Any],
        *,
        idempotency_key: str | None = None,
        producer_execution_key: str | None = None,
        requires_stage_id: bool = False,
        captures_resolution: bool = False,
    ) -> str:
        key = idempotency_key or str(uuid.uuid4())
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        with self._connect() as db:
            db.execute(
                """
                INSERT OR IGNORE INTO outbox
                (
                    idempotency_key, method, path, payload_json, created_at,
                    producer_execution_key, requires_stage_id, captures_resolution
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    key,
                    method.upper(),
                    path,
                    encoded,
                    time.time(),
                    producer_execution_key,
                    int(requires_stage_id),
                    int(captures_resolution),
                ),
            )
        return key

    def pending(self, *, limit: int = 100, now: float | None = None) -> list[OutboxItem]:
        timestamp = time.time() if now is None else now
        with self._connect() as db:
            rows = db.execute(
                """
                SELECT
                    id, idempotency_key, method, path, payload_json, attempts,
                    producer_execution_key, requires_stage_id, captures_resolution,
                    next_attempt_at
                FROM outbox
                WHERE status='pending'
                ORDER BY id LIMIT ?
                """,
                (limit,),
            ).fetchall()
        # Preserve create -> events -> telemetry -> artifacts -> finish order
        # across retries, including while the first request is backing off.
        due_rows = []
        for row in rows:
            if float(row["next_attempt_at"]) > timestamp:
                break
            due_rows.append(row)
        return [
            OutboxItem(
                id=int(row["id"]),
                idempotency_key=str(row["idempotency_key"]),
                method=str(row["method"]),
                path=str(row["path"]),
                payload=json.loads(row["payload_json"]),
                attempts=int(row["attempts"]),
                producer_execution_key=(
                    str(row["producer_execution_key"])
                    if row["producer_execution_key"] is not None
                    else None
                ),
                requires_stage_id=bool(row["requires_stage_id"]),
                captures_resolution=bool(row["captures_resolution"]),
            )
            for row in due_rows
        ]

    def mark_sent(self, item_id: int) -> None:
        with self._connect() as db:
            db.execute("UPDATE outbox SET status='sent', last_error=NULL WHERE id=?", (item_id,))

    def mark_failed(self, item_id: int, error: str, attempts: int) -> None:
        delay = min(300.0, 2.0 ** min(attempts, 8))
        with self._connect() as db:
            db.execute(
                """
                UPDATE outbox
                SET attempts=?, next_attempt_at=?, last_error=?
                WHERE id=?
                """,
                (attempts, time.time() + delay, error[:1000], item_id),
            )

    def count(self, status: str = "pending") -> int:
        with self._connect() as db:
            row = db.execute("SELECT COUNT(*) FROM outbox WHERE status=?", (status,)).fetchone()
        return int(row[0])

    def save_execution_resolution(
        self,
        producer_execution_key: str,
        *,
        execution_id: str,
        stage_id: str,
    ) -> None:
        with self._connect() as db:
            existing = db.execute(
                """
                SELECT execution_id, stage_id
                FROM execution_resolution
                WHERE producer_execution_key=?
                """,
                (producer_execution_key,),
            ).fetchone()
            if existing is not None and (
                str(existing["execution_id"]) != execution_id
                or str(existing["stage_id"]) != stage_id
            ):
                raise ValueError("create replay returned a conflicting execution/stage mapping")
            db.execute(
                """
                INSERT INTO execution_resolution(
                    producer_execution_key, execution_id, stage_id, updated_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(producer_execution_key) DO UPDATE SET
                    execution_id=excluded.execution_id,
                    stage_id=excluded.stage_id,
                    updated_at=excluded.updated_at
                """,
                (producer_execution_key, execution_id, stage_id, time.time()),
            )

    def get_execution_resolution(self, producer_execution_key: str) -> dict[str, str] | None:
        with self._connect() as db:
            row = db.execute(
                """
                SELECT execution_id, stage_id
                FROM execution_resolution
                WHERE producer_execution_key=?
                """,
                (producer_execution_key,),
            ).fetchone()
        if row is None:
            return None
        return {
            "execution_id": str(row["execution_id"]),
            "stage_id": str(row["stage_id"]),
        }

    def flush(self, sender: Callable[[OutboxItem], None], *, limit: int = 100) -> tuple[int, int]:
        sent = failed = 0
        for item in self.pending(limit=limit):
            try:
                sender(item)
            except Exception as exc:
                self.mark_failed(item.id, str(exc), item.attempts + 1)
                failed += 1
                break
            else:
                self.mark_sent(item.id)
                sent += 1
        return sent, failed
