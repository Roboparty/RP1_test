from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Path, status

from app.core.auth import RequestActor, require_ingestion_service
from app.core.db import DbSession
from app.repositories.ingestion import IngestionRepository
from app.schemas.common import DataEnvelope
from app.schemas.ingestion import (
    ArtifactMetadataRequest,
    EventBatchRequest,
    ExecutionRegisterRequest,
    ExecutionRegistration,
    FinishExecutionRequest,
    HeartbeatRequest,
    IngestionResult,
    PRODUCER_EXECUTION_KEY_PATTERN,
    TelemetryBatchRequest,
)
from app.services.ingestion import IngestionService


router = APIRouter(prefix="/api/v1/ingestion", tags=["ingestion"])
IdempotencyKey = Annotated[
    str,
    Header(alias="Idempotency-Key", min_length=8, max_length=200),
]
ServiceActor = Annotated[RequestActor, Depends(require_ingestion_service)]
ProducerExecutionKey = Annotated[
    str,
    Path(
        min_length=1,
        max_length=200,
        pattern=PRODUCER_EXECUTION_KEY_PATTERN,
        description=(
            "Stable producer execution key, resolved only inside the "
            "authenticated ingestion source."
        ),
    ),
]


def _service(session: DbSession) -> IngestionService:
    return IngestionService(IngestionRepository(session))


@router.post(
    "/executions",
    response_model=DataEnvelope[ExecutionRegistration],
    status_code=status.HTTP_201_CREATED,
)
def register_execution(
    payload: ExecutionRegisterRequest,
    _: ServiceActor,
    session: DbSession,
    idempotency_key: IdempotencyKey,
) -> DataEnvelope[ExecutionRegistration]:
    return DataEnvelope(
        data=_service(session).register_execution(payload, idempotency_key)
    )


@router.post(
    "/executions/{execution_id}/heartbeat",
    response_model=DataEnvelope[IngestionResult],
)
def heartbeat(
    execution_id: UUID,
    payload: HeartbeatRequest,
    _: ServiceActor,
    session: DbSession,
    idempotency_key: IdempotencyKey,
) -> DataEnvelope[IngestionResult]:
    return DataEnvelope(
        data=_service(session).heartbeat(
            execution_id, payload, idempotency_key
        )
    )


@router.post(
    "/executions/{execution_id}/events",
    response_model=DataEnvelope[IngestionResult],
)
def ingest_events(
    execution_id: UUID,
    payload: EventBatchRequest,
    _: ServiceActor,
    session: DbSession,
    idempotency_key: IdempotencyKey,
) -> DataEnvelope[IngestionResult]:
    return DataEnvelope(
        data=_service(session).events(
            execution_id, payload, idempotency_key
        )
    )


@router.post(
    "/executions/{execution_id}/telemetry",
    response_model=DataEnvelope[IngestionResult],
)
def ingest_telemetry(
    execution_id: UUID,
    payload: TelemetryBatchRequest,
    _: ServiceActor,
    session: DbSession,
    idempotency_key: IdempotencyKey,
) -> DataEnvelope[IngestionResult]:
    return DataEnvelope(
        data=_service(session).telemetry(
            execution_id, payload, idempotency_key
        )
    )


@router.post(
    "/executions/{execution_id}/artifacts",
    response_model=DataEnvelope[IngestionResult],
)
def register_artifact_metadata(
    execution_id: UUID,
    payload: ArtifactMetadataRequest,
    _: ServiceActor,
    session: DbSession,
    idempotency_key: IdempotencyKey,
) -> DataEnvelope[IngestionResult]:
    return DataEnvelope(
        data=_service(session).artifact(
            execution_id, payload, idempotency_key
        )
    )


@router.post(
    "/executions/{execution_id}/finish",
    response_model=DataEnvelope[IngestionResult],
)
def finish_execution(
    execution_id: UUID,
    payload: FinishExecutionRequest,
    _: ServiceActor,
    session: DbSession,
    idempotency_key: IdempotencyKey,
) -> DataEnvelope[IngestionResult]:
    return DataEnvelope(
        data=_service(session).finish(
            execution_id, payload, idempotency_key
        )
    )


@router.post(
    "/executions/by-producer/{producer_execution_key}/heartbeat",
    response_model=DataEnvelope[IngestionResult],
)
def heartbeat_by_producer(
    producer_execution_key: ProducerExecutionKey,
    payload: HeartbeatRequest,
    _: ServiceActor,
    session: DbSession,
    idempotency_key: IdempotencyKey,
) -> DataEnvelope[IngestionResult]:
    return DataEnvelope(
        data=_service(session).heartbeat_by_producer(
            producer_execution_key, payload, idempotency_key
        )
    )


@router.post(
    "/executions/by-producer/{producer_execution_key}/events",
    response_model=DataEnvelope[IngestionResult],
)
def ingest_events_by_producer(
    producer_execution_key: ProducerExecutionKey,
    payload: EventBatchRequest,
    _: ServiceActor,
    session: DbSession,
    idempotency_key: IdempotencyKey,
) -> DataEnvelope[IngestionResult]:
    return DataEnvelope(
        data=_service(session).events_by_producer(
            producer_execution_key, payload, idempotency_key
        )
    )


@router.post(
    "/executions/by-producer/{producer_execution_key}/telemetry",
    response_model=DataEnvelope[IngestionResult],
)
def ingest_telemetry_by_producer(
    producer_execution_key: ProducerExecutionKey,
    payload: TelemetryBatchRequest,
    _: ServiceActor,
    session: DbSession,
    idempotency_key: IdempotencyKey,
) -> DataEnvelope[IngestionResult]:
    return DataEnvelope(
        data=_service(session).telemetry_by_producer(
            producer_execution_key, payload, idempotency_key
        )
    )


@router.post(
    "/executions/by-producer/{producer_execution_key}/artifacts",
    response_model=DataEnvelope[IngestionResult],
)
def register_artifact_metadata_by_producer(
    producer_execution_key: ProducerExecutionKey,
    payload: ArtifactMetadataRequest,
    _: ServiceActor,
    session: DbSession,
    idempotency_key: IdempotencyKey,
) -> DataEnvelope[IngestionResult]:
    return DataEnvelope(
        data=_service(session).artifact_by_producer(
            producer_execution_key, payload, idempotency_key
        )
    )


@router.post(
    "/executions/by-producer/{producer_execution_key}/finish",
    response_model=DataEnvelope[IngestionResult],
)
def finish_execution_by_producer(
    producer_execution_key: ProducerExecutionKey,
    payload: FinishExecutionRequest,
    _: ServiceActor,
    session: DbSession,
    idempotency_key: IdempotencyKey,
) -> DataEnvelope[IngestionResult]:
    return DataEnvelope(
        data=_service(session).finish_by_producer(
            producer_execution_key, payload, idempotency_key
        )
    )
