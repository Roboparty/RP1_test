from __future__ import annotations

from typing import Any
from uuid import UUID

from app.repositories.ingestion import IngestionRepository


class IngestionService:
    """Transaction-scoped orchestration for the edge collector contract."""

    def __init__(self, repository: IngestionRepository) -> None:
        self.repository = repository

    def register_execution(self, payload: Any, idempotency_key: str) -> Any:
        return self.repository.register_execution(payload, idempotency_key)

    def heartbeat(
        self, execution_id: UUID, payload: Any, idempotency_key: str
    ) -> Any:
        return self.repository.heartbeat(
            execution_id, payload, idempotency_key
        )

    def heartbeat_by_producer(
        self,
        producer_execution_key: str,
        payload: Any,
        idempotency_key: str,
    ) -> Any:
        return self.heartbeat(
            self.repository.execution_id_by_producer(producer_execution_key),
            payload,
            idempotency_key,
        )

    def events(
        self, execution_id: UUID, payload: Any, idempotency_key: str
    ) -> Any:
        return self.repository.ingest_events(
            execution_id, payload, idempotency_key
        )

    def events_by_producer(
        self,
        producer_execution_key: str,
        payload: Any,
        idempotency_key: str,
    ) -> Any:
        return self.events(
            self.repository.execution_id_by_producer(producer_execution_key),
            payload,
            idempotency_key,
        )

    def telemetry(
        self, execution_id: UUID, payload: Any, idempotency_key: str
    ) -> Any:
        return self.repository.ingest_telemetry(
            execution_id, payload, idempotency_key
        )

    def telemetry_by_producer(
        self,
        producer_execution_key: str,
        payload: Any,
        idempotency_key: str,
    ) -> Any:
        return self.telemetry(
            self.repository.execution_id_by_producer(producer_execution_key),
            payload,
            idempotency_key,
        )

    def artifact(
        self, execution_id: UUID, payload: Any, idempotency_key: str
    ) -> Any:
        return self.repository.register_artifact(
            execution_id, payload, idempotency_key
        )

    def artifact_by_producer(
        self,
        producer_execution_key: str,
        payload: Any,
        idempotency_key: str,
    ) -> Any:
        return self.artifact(
            self.repository.execution_id_by_producer(producer_execution_key),
            payload,
            idempotency_key,
        )

    def finish(
        self, execution_id: UUID, payload: Any, idempotency_key: str
    ) -> Any:
        return self.repository.finish_execution(
            execution_id, payload, idempotency_key
        )

    def finish_by_producer(
        self,
        producer_execution_key: str,
        payload: Any,
        idempotency_key: str,
    ) -> Any:
        return self.finish(
            self.repository.execution_id_by_producer(producer_execution_key),
            payload,
            idempotency_key,
        )
