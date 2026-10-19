"""Bearer-authenticated ingestion client; all remote writes pass through outbox."""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from typing import Any
from uuid import UUID

from .outbox import OutboxItem, SQLiteOutbox

INGESTION_ROOT = "/api/v1/ingestion"
EXECUTIONS_PATH = f"{INGESTION_ROOT}/executions"
BY_PRODUCER_ROOT = f"{EXECUTIONS_PATH}/by-producer"
PRODUCER_EXECUTION_KEY_PATTERN = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,199}$"
)


class IngestionClient:
    def __init__(self, base_url: str, token: str, outbox: SQLiteOutbox) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token.strip()
        self.outbox = outbox

    def _queue(
        self,
        method: str,
        path: str,
        payload: dict[str, Any],
        idempotency_key: str,
        *,
        producer_execution_key: str | None = None,
        requires_stage_id: bool = False,
        captures_resolution: bool = False,
    ) -> str:
        if not path.startswith(INGESTION_ROOT + "/"):
            raise ValueError("platform path must use /api/v1/ingestion")
        return self.outbox.enqueue(
            method,
            path,
            payload,
            idempotency_key=idempotency_key,
            producer_execution_key=producer_execution_key,
            requires_stage_id=requires_stage_id,
            captures_resolution=captures_resolution,
        )

    @staticmethod
    def validate_producer_execution_key(producer_execution_key: str) -> str:
        value = producer_execution_key.strip()
        if not PRODUCER_EXECUTION_KEY_PATTERN.fullmatch(value):
            raise ValueError(
                "producer_execution_key must match "
                "^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,199}$"
            )
        return value

    @classmethod
    def _producer_path(cls, producer_execution_key: str, operation: str) -> str:
        value = cls.validate_producer_execution_key(producer_execution_key)
        if operation not in {"heartbeat", "events", "telemetry", "artifacts", "finish"}:
            raise ValueError(f"unsupported ingestion operation: {operation}")
        return f"{BY_PRODUCER_ROOT}/{value}/{operation}"

    def create_execution(self, payload: dict[str, Any], key: str) -> str:
        producer_execution_key = self.validate_producer_execution_key(
            str(payload.get("producer_key", ""))
        )
        return self._queue(
            "POST",
            EXECUTIONS_PATH,
            payload,
            key,
            producer_execution_key=producer_execution_key,
            captures_resolution=True,
        )

    def heartbeat(
        self, producer_execution_key: str, payload: dict[str, Any], key: str
    ) -> str:
        return self._queue(
            "POST",
            self._producer_path(producer_execution_key, "heartbeat"),
            payload,
            key,
            producer_execution_key=producer_execution_key,
        )

    def events(
        self, producer_execution_key: str, payload: dict[str, Any], key: str
    ) -> str:
        return self._queue(
            "POST",
            self._producer_path(producer_execution_key, "events"),
            payload,
            key,
            producer_execution_key=producer_execution_key,
        )

    def telemetry(
        self,
        producer_execution_key: str,
        payload: dict[str, Any],
        key: str,
    ) -> str:
        return self._queue(
            "POST",
            self._producer_path(producer_execution_key, "telemetry"),
            payload,
            key,
            producer_execution_key=producer_execution_key,
            requires_stage_id=True,
        )

    def finish_execution(
        self, producer_execution_key: str, payload: dict[str, Any], key: str
    ) -> str:
        return self._queue(
            "POST",
            self._producer_path(producer_execution_key, "finish"),
            payload,
            key,
            producer_execution_key=producer_execution_key,
        )

    def artifact(
        self, producer_execution_key: str, payload: dict[str, Any], key: str
    ) -> str:
        return self._queue(
            "POST",
            self._producer_path(producer_execution_key, "artifacts"),
            payload,
            key,
            producer_execution_key=producer_execution_key,
        )

    def send(self, item: OutboxItem) -> None:
        if not self.token:
            raise RuntimeError("API Bearer token is required to flush the outbox")
        payload = dict(item.payload)
        if item.requires_stage_id:
            if item.producer_execution_key is None:
                raise RuntimeError("telemetry outbox item has no producer execution key")
            resolution = self.outbox.get_execution_resolution(
                item.producer_execution_key
            )
            if resolution is None:
                raise RuntimeError(
                    "create execution response has not resolved telemetry stage_id"
                )
            payload["stage_id"] = resolution["stage_id"]
        request = urllib.request.Request(
            f"{self.base_url}{item.path}",
            data=json.dumps(
                payload, ensure_ascii=False, allow_nan=False
            ).encode("utf-8"),
            method=item.method,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
                "Idempotency-Key": item.idempotency_key,
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=8.0) as response:
                raw_response = response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"ingestion HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"ingestion unavailable: {exc.reason}") from exc
        if item.captures_resolution:
            if item.producer_execution_key is None:
                raise RuntimeError("create outbox item has no producer execution key")
            try:
                body = json.loads(raw_response.decode("utf-8"))
                data = body["data"]
                execution_id = str(UUID(str(data["execution_id"])))
                stage_id = str(UUID(str(data["stage_id"])))
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                raise RuntimeError(
                    "create response missing valid data.execution_id/data.stage_id"
                ) from exc
            self.outbox.save_execution_resolution(
                item.producer_execution_key,
                execution_id=execution_id,
                stage_id=stage_id,
            )

    def flush(self, *, limit: int = 100) -> tuple[int, int]:
        return self.outbox.flush(self.send, limit=limit)
