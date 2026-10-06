"""Crash-safe local upload outbox for execution bundles."""

from __future__ import annotations

import json
import random
import sqlite3
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping


OUTBOX_STATES = {
    "local_completed",
    "uploading",
    "validating",
    "pending_review",
    "approved",
    "rejected",
    "sync_failed",
}
TERMINAL_STATES = {"approved", "rejected"}


@dataclass(frozen=True)
class OutboxItem:
    execution_uuid: str
    test_id: str
    bundle_path: str
    manifest: dict[str, Any]
    state: str
    attempts: int
    next_attempt_at: float
    last_error: str
    submission_id: str
    upload_id: str
    next_chunk_index: int
    review_reason: str
    remote_state: str
    created_at: float
    updated_at: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class Outbox:
    """SQLite-backed queue that survives gateway and workstation restarts."""

    def __init__(
        self,
        path: Path | str,
        *,
        recover_interrupted: bool = True,
    ) -> None:
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._initialize(recover_interrupted=recover_interrupted)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _initialize(self, *, recover_interrupted: bool) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS execution_outbox (
                    execution_uuid TEXT PRIMARY KEY,
                    test_id TEXT NOT NULL,
                    bundle_path TEXT NOT NULL,
                    manifest_json TEXT NOT NULL,
                    state TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at REAL NOT NULL DEFAULT 0,
                    last_error TEXT NOT NULL DEFAULT '',
                    submission_id TEXT NOT NULL DEFAULT '',
                    upload_id TEXT NOT NULL DEFAULT '',
                    next_chunk_index INTEGER NOT NULL DEFAULT 0,
                    review_reason TEXT NOT NULL DEFAULT '',
                    remote_state TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS execution_outbox_due
                ON execution_outbox(state, next_attempt_at);
                """
            )
            if recover_interrupted:
                self._recover_interrupted(connection)

    @staticmethod
    def _recover_interrupted(connection: sqlite3.Connection) -> int:
        # An interrupted upload remains resumable from its recorded chunk.
        cursor = connection.execute(
            """
            UPDATE execution_outbox
            SET state = 'sync_failed',
                next_attempt_at = 0,
                last_error = CASE
                    WHEN last_error = '' THEN 'uploader restarted'
                    ELSE last_error
                END
            WHERE state IN ('uploading', 'validating')
            """
        )
        return max(0, cursor.rowcount)

    def recover_interrupted(self) -> int:
        """Make work left by a stopped uploader eligible for resumption."""
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            return self._recover_interrupted(connection)

    def import_from(self, source_path: Path | str) -> int:
        """Copy legacy outbox rows without overwriting existing global rows."""
        source = Path(source_path).expanduser().resolve()
        if source == self.path or not source.is_file():
            return 0
        columns = (
            "execution_uuid",
            "test_id",
            "bundle_path",
            "manifest_json",
            "state",
            "attempts",
            "next_attempt_at",
            "last_error",
            "submission_id",
            "upload_id",
            "next_chunk_index",
            "review_reason",
            "remote_state",
            "created_at",
            "updated_at",
        )
        column_list = ", ".join(columns)
        with self._lock:
            connection = self._connect()
            attached = False
            try:
                connection.execute(
                    "ATTACH DATABASE ? AS legacy_outbox",
                    (str(source),),
                )
                attached = True
                available = {
                    str(row["name"])
                    for row in connection.execute(
                        "PRAGMA legacy_outbox.table_info(execution_outbox)"
                    )
                }
                missing = set(columns) - available
                if missing:
                    raise ValueError(
                        f"legacy outbox is missing columns: {sorted(missing)}"
                    )
                connection.execute("BEGIN IMMEDIATE")
                cursor = connection.execute(
                    f"""
                    INSERT OR IGNORE INTO execution_outbox ({column_list})
                    SELECT {column_list}
                    FROM legacy_outbox.execution_outbox
                    """
                )
                connection.commit()
                return max(0, cursor.rowcount)
            finally:
                if connection.in_transaction:
                    connection.rollback()
                if attached:
                    connection.execute("DETACH DATABASE legacy_outbox")
                connection.close()

    @staticmethod
    def _item(row: sqlite3.Row) -> OutboxItem:
        return OutboxItem(
            execution_uuid=str(row["execution_uuid"]),
            test_id=str(row["test_id"]),
            bundle_path=str(row["bundle_path"]),
            manifest=json.loads(str(row["manifest_json"])),
            state=str(row["state"]),
            attempts=int(row["attempts"]),
            next_attempt_at=float(row["next_attempt_at"]),
            last_error=str(row["last_error"]),
            submission_id=str(row["submission_id"]),
            upload_id=str(row["upload_id"]),
            next_chunk_index=int(row["next_chunk_index"]),
            review_reason=str(row["review_reason"]),
            remote_state=str(row["remote_state"]),
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
        )

    def enqueue(
        self,
        manifest: Mapping[str, Any],
        bundle_path: Path | str,
    ) -> OutboxItem:
        execution_uuid = str(manifest.get("execution_uuid") or "").strip()
        test_id = str(manifest.get("test_id") or "").strip()
        path = Path(bundle_path).expanduser().resolve()
        if not execution_uuid or not test_id:
            raise ValueError("manifest execution_uuid and test_id are required")
        if not path.is_file():
            raise FileNotFoundError(path)
        now = time.time()
        payload = json.dumps(
            dict(manifest),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO execution_outbox (
                    execution_uuid, test_id, bundle_path, manifest_json,
                    state, next_attempt_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'local_completed', 0, ?, ?)
                ON CONFLICT(execution_uuid) DO UPDATE SET
                    test_id = excluded.test_id,
                    bundle_path = excluded.bundle_path,
                    manifest_json = excluded.manifest_json,
                    state = CASE
                        WHEN execution_outbox.state IN ('approved', 'pending_review')
                            THEN execution_outbox.state
                        ELSE 'local_completed'
                    END,
                    next_attempt_at = 0,
                    last_error = '',
                    updated_at = excluded.updated_at
                """,
                (execution_uuid, test_id, str(path), payload, now, now),
            )
        item = self.get(execution_uuid)
        assert item is not None
        return item

    def get(self, execution_uuid: str) -> OutboxItem | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM execution_outbox WHERE execution_uuid = ?",
                (execution_uuid,),
            ).fetchone()
        return self._item(row) if row is not None else None

    def get_by_test_id(self, test_id: str) -> OutboxItem | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM execution_outbox
                WHERE test_id = ?
                ORDER BY updated_at DESC
                LIMIT 1
                """,
                (test_id,),
            ).fetchone()
        return self._item(row) if row is not None else None

    def list(self, *, limit: int = 100) -> list[OutboxItem]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM execution_outbox
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (max(1, min(int(limit), 1000)),),
            ).fetchall()
        return [self._item(row) for row in rows]

    def due(self, *, now: float | None = None, limit: int = 10) -> list[OutboxItem]:
        current = time.time() if now is None else float(now)
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM execution_outbox
                WHERE state IN ('local_completed', 'sync_failed', 'validating',
                                'pending_review')
                  AND next_attempt_at <= ?
                ORDER BY updated_at ASC
                LIMIT ?
                """,
                (current, max(1, min(int(limit), 100))),
            ).fetchall()
        return [self._item(row) for row in rows]

    def update(self, execution_uuid: str, **changes: Any) -> OutboxItem:
        allowed = {
            "state",
            "attempts",
            "next_attempt_at",
            "last_error",
            "submission_id",
            "upload_id",
            "next_chunk_index",
            "review_reason",
            "remote_state",
        }
        unknown = set(changes) - allowed
        if unknown:
            raise ValueError(f"unsupported outbox fields: {sorted(unknown)}")
        if "state" in changes and changes["state"] not in OUTBOX_STATES:
            raise ValueError(f"unsupported outbox state: {changes['state']}")
        if not changes:
            item = self.get(execution_uuid)
            if item is None:
                raise KeyError(execution_uuid)
            return item
        changes["updated_at"] = time.time()
        assignments = ", ".join(f"{name} = ?" for name in changes)
        values = [changes[name] for name in changes]
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                f"UPDATE execution_outbox SET {assignments} WHERE execution_uuid = ?",
                (*values, execution_uuid),
            )
            if cursor.rowcount != 1:
                raise KeyError(execution_uuid)
        item = self.get(execution_uuid)
        assert item is not None
        return item

    def fail(
        self,
        execution_uuid: str,
        error: str,
        *,
        base_delay_s: float = 5.0,
        max_delay_s: float = 900.0,
        random_source: random.Random | None = None,
    ) -> OutboxItem:
        item = self.get(execution_uuid)
        if item is None:
            raise KeyError(execution_uuid)
        attempts = item.attempts + 1
        delay = min(max_delay_s, base_delay_s * (2 ** min(attempts - 1, 10)))
        jitter = (random_source or random).uniform(0.5, 1.5)
        return self.update(
            execution_uuid,
            state="sync_failed",
            attempts=attempts,
            next_attempt_at=time.time() + delay * jitter,
            last_error=str(error)[:4096],
        )

    def retry(self, execution_uuid: str) -> OutboxItem:
        item = self.get(execution_uuid)
        if item is None:
            raise KeyError(execution_uuid)
        if item.state == "approved":
            return item
        return self.update(
            execution_uuid,
            state="local_completed",
            next_attempt_at=0,
            last_error="",
        )


__all__ = [
    "OUTBOX_STATES",
    "TERMINAL_STATES",
    "Outbox",
    "OutboxItem",
]
