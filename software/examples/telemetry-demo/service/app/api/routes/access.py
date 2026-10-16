from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, status
from sqlalchemy.exc import IntegrityError

from app.core.auth import RequestActorDep
from app.core.db import DbSession
from app.core.errors import ApiError
from app.repositories.security import SecurityRepository
from app.schemas.common import DataEnvelope
from app.schemas.security import AccessRequest, AccessRequestCreate, AvailableCampaign


router = APIRouter(prefix="/api/v1", tags=["campaign-access"])


def _access_request(row: dict) -> AccessRequest:
    return AccessRequest(
        id=row["public_id"],
        requester_id=row.get("requester_public_id"),
        requester_username=row.get("requester_username"),
        campaign_id=row["campaign_public_id"],
        campaign_code=row["campaign_code"],
        campaign_name=row["campaign_name"],
        requested_level=row["requested_level"],
        reason=row["reason"],
        status=row["status"],
        decision_reason=row["decision_reason"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        decided_at=row["decided_at"],
        decided_by=row.get("decided_by_public_id"),
    )


@router.get(
    "/campaign-access/available",
    response_model=DataEnvelope[list[AvailableCampaign]],
)
def available_campaigns(session: DbSession) -> DataEnvelope[list[AvailableCampaign]]:
    items = [
        AvailableCampaign(
            campaign_id=row["campaign_public_id"],
            campaign_code=row["campaign_code"],
            campaign_name=row["campaign_name"],
            asset_kind=row["asset_kind"],
            current_grant=row["current_grant"],
            pending_request_id=row["pending_request_public_id"],
            pending_requested_level=row["pending_requested_level"],
        )
        for row in SecurityRepository(session).available_campaigns()
    ]
    return DataEnvelope(data=items)


@router.get(
    "/access-requests/mine",
    response_model=DataEnvelope[list[AccessRequest]],
)
def my_access_requests(session: DbSession) -> DataEnvelope[list[AccessRequest]]:
    return DataEnvelope(
        data=[
            _access_request(row)
            for row in SecurityRepository(session).my_access_requests()
        ]
    )


@router.post(
    "/access-requests",
    response_model=DataEnvelope[AccessRequest],
    status_code=status.HTTP_201_CREATED,
)
def submit_access_request(
    payload: AccessRequestCreate,
    actor: RequestActorDep,
    session: DbSession,
) -> DataEnvelope[AccessRequest]:
    repository = SecurityRepository(session)
    try:
        request_id = repository.submit_access_request(
            payload.campaign_id,
            payload.requested_level,
            payload.reason,
            actor.request_id,
            actor.source_ip,
        )
    except IntegrityError as exc:
        if getattr(exc.orig, "sqlstate", None) == "23505":
            raise ApiError(
                409,
                "pending_request_exists",
                "该账户已对该Campaign提交待审批申请。",
            ) from exc
        raise
    except Exception as exc:
        sqlstate = getattr(getattr(exc, "orig", None), "sqlstate", None)
        if sqlstate == "P0002":
            raise ApiError(404, "not_found", "Campaign不存在或当前不可申请。") from exc
        if sqlstate == "P0001":
            raise ApiError(
                409,
                "conflict",
                "当前授权已满足该申请级别。",
            ) from exc
        raise
    row = next(
        item
        for item in repository.my_access_requests()
        if UUID(str(item["public_id"])) == request_id
    )
    return DataEnvelope(data=_access_request(row))


@router.post(
    "/access-requests/{access_request_id}/cancel",
    response_model=DataEnvelope[AccessRequest],
)
def cancel_access_request(
    access_request_id: UUID,
    actor: RequestActorDep,
    session: DbSession,
) -> DataEnvelope[AccessRequest]:
    repository = SecurityRepository(session)
    if not repository.cancel_access_request(
        access_request_id, actor.request_id, actor.source_ip
    ):
        raise ApiError(
            404,
            "not_found",
            "待取消的访问申请不存在、不可见或已处理。",
        )
    row = next(
        item
        for item in repository.my_access_requests()
        if UUID(str(item["public_id"])) == access_request_id
    )
    return DataEnvelope(data=_access_request(row))
