from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header, Response, status

from app.core.auth import RequestActorDep
from app.core.db import DbSession
from app.core.errors import ApiError
from app.domain.mtbf import IMPLEMENTATION_KEY, MtbfInputError, calculate_mtbf
from app.repositories.mtbf import MtbfRepository
from app.schemas.common import DataEnvelope
from app.schemas.mtbf import (
    CalculationRunView,
    CampaignMtbfConfigRequest,
    CampaignMtbfConfigView,
    CampaignMtbfResultView,
    FailureBreakdownView,
    InterruptionUpdateRequest,
    InterruptionView,
    MtbfCalculationView,
    MtbfPreviewRequest,
    RecalculateRequest,
    RecalculateResponse,
    TimeBreakdownView,
)


router = APIRouter(prefix="/api/v1", tags=["mtbf"])
IdempotencyKey = Annotated[
    str,
    Header(alias="Idempotency-Key", min_length=8, max_length=200),
]
RequiredIfMatch = Annotated[str, Header(alias="If-Match")]
OptionalIfMatch = Annotated[str | None, Header(alias="If-Match")]


@router.post("/calculations/preview", response_model=DataEnvelope[MtbfCalculationView])
def preview(
    payload: MtbfPreviewRequest,
    _: RequestActorDep,
) -> DataEnvelope[MtbfCalculationView]:
    levels = sorted(set(payload.confidence_levels + [0.70, 0.90]))
    try:
        result = calculate_mtbf(
            payload.exposure_hours,
            payload.relevant_failure_count,
            levels,
        )
    except MtbfInputError as exc:
        raise ApiError(422, "validation_error", str(exc)) from exc
    return DataEnvelope(
        data=MtbfCalculationView(
            method=IMPLEMENTATION_KEY,
            exposure_hours=result.exposure_hours,
            relevant_failure_count=result.relevant_failure_count,
            point_estimate_hours=result.point_estimate_hours,
            point_estimate_status=result.point_estimate_status,
            lower_70_hours=result.lower_bound(0.70),
            lower_90_hours=result.lower_bound(0.90),
            confidence_bounds={str(level): result.lower_bound(level) for level in levels},
            formal=False,
        )
    )


@router.put(
    "/campaigns/{campaign_id}/mtbf-config/{scope_code}",
    response_model=DataEnvelope[CampaignMtbfConfigView],
)
def upsert_config(
    campaign_id: UUID,
    scope_code: str,
    payload: CampaignMtbfConfigRequest,
    response: Response,
    session: DbSession,
    idempotency_key: IdempotencyKey,
    if_match: OptionalIfMatch = None,
) -> DataEnvelope[CampaignMtbfConfigView]:
    del idempotency_key  # PUT is state-idempotent; unchanged values do not fire recomputation.
    config = MtbfRepository(session).upsert_config(campaign_id, scope_code, payload, if_match)
    response.headers["ETag"] = config.etag
    return DataEnvelope(data=config)


@router.get(
    "/campaigns/{campaign_id}/mtbf-config/{scope_code}",
    response_model=DataEnvelope[CampaignMtbfConfigView],
)
def get_config(
    campaign_id: UUID,
    scope_code: str,
    response: Response,
    session: DbSession,
) -> DataEnvelope[CampaignMtbfConfigView]:
    config = MtbfRepository(session).get_config(campaign_id, scope_code)
    response.headers["ETag"] = config.etag
    return DataEnvelope(data=config)


@router.get(
    "/campaigns/{campaign_id}/mtbf/{scope_code}",
    response_model=DataEnvelope[CampaignMtbfResultView],
)
def get_current_result(
    campaign_id: UUID,
    scope_code: str,
    session: DbSession,
) -> DataEnvelope[CampaignMtbfResultView]:
    return DataEnvelope(data=MtbfRepository(session).get_current_result(campaign_id, scope_code))


@router.get(
    "/campaigns/{campaign_id}/mtbf/{scope_code}/time-breakdown",
    response_model=DataEnvelope[TimeBreakdownView],
)
def get_time_breakdown(
    campaign_id: UUID,
    scope_code: str,
    session: DbSession,
) -> DataEnvelope[TimeBreakdownView]:
    return DataEnvelope(data=MtbfRepository(session).get_time_breakdown(campaign_id, scope_code))


@router.get(
    "/campaigns/{campaign_id}/mtbf/{scope_code}/failures",
    response_model=DataEnvelope[FailureBreakdownView],
)
def get_failures(
    campaign_id: UUID,
    scope_code: str,
    session: DbSession,
) -> DataEnvelope[FailureBreakdownView]:
    return DataEnvelope(data=MtbfRepository(session).get_failures(campaign_id, scope_code))


@router.patch(
    "/interruptions/{interruption_id}",
    response_model=DataEnvelope[InterruptionView],
)
def update_interruption(
    interruption_id: UUID,
    payload: InterruptionUpdateRequest,
    response: Response,
    session: DbSession,
    if_match: RequiredIfMatch,
    idempotency_key: IdempotencyKey,
) -> DataEnvelope[InterruptionView]:
    del idempotency_key
    interruption = MtbfRepository(session).update_interruption(interruption_id, payload, if_match)
    response.headers["ETag"] = interruption.etag
    return DataEnvelope(data=interruption)


@router.post(
    "/campaigns/{campaign_id}/mtbf/{scope_code}/recalculate",
    response_model=DataEnvelope[RecalculateResponse],
    status_code=status.HTTP_202_ACCEPTED,
)
def recalculate(
    campaign_id: UUID,
    scope_code: str,
    payload: RecalculateRequest,
    session: DbSession,
    idempotency_key: IdempotencyKey,
) -> DataEnvelope[RecalculateResponse]:
    return DataEnvelope(
        data=MtbfRepository(session).queue_recalculation(
            campaign_id, scope_code, payload.reason, idempotency_key
        )
    )


@router.get("/calculation-runs/{run_id}", response_model=DataEnvelope[CalculationRunView])
def get_calculation_run(
    run_id: UUID,
    session: DbSession,
) -> DataEnvelope[CalculationRunView]:
    return DataEnvelope(data=MtbfRepository(session).get_calculation_run(run_id))
