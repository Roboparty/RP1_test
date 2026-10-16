from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy.exc import IntegrityError

from app.api.routes.access import _access_request
from app.core.auth import RequestActor, require_roles
from app.core.db import DbSession
from app.core.errors import ApiError
from app.repositories.security import SecurityRepository
from app.schemas.common import DataEnvelope
from app.schemas.security import (
    AccessDecision,
    AccessRequest,
    AuditEvent,
    CampaignAccess,
    CampaignGrantRequest,
    ReasonRequest,
    ServiceCredentialCreateRequest,
    ServiceCredentialGrant,
    ServiceCredentialRevocation,
    ServiceCredentialRevokeRequest,
    ServiceCredentialRotateRequest,
    UserCreateRequest,
    UserView,
)
from app.services.auth import AuthService, ServiceCredentialService
from app.core.config import get_settings


router = APIRouter(prefix="/api/v1/admin", tags=["admin"])
AdminActor = Annotated[
    RequestActor,
    Depends(require_roles("SYSTEM_ADMIN")),
]
RequestStatus = Annotated[
    Literal["PENDING", "APPROVED", "REJECTED", "CANCELLED"] | None,
    Query(alias="status"),
]


def _user(row: dict) -> UserView:
    return UserView(
        id=row["public_id"],
        username=row["username"],
        display_name=row["display_name"],
        role=row["role"],
        kind=row["principal_kind"],
        enabled=row["enabled"],
        must_change_password=row["must_change_password"],
        created_at=row["created_at"],
        disabled_at=row["disabled_at"],
    )


def _grant(row: dict) -> CampaignAccess:
    return CampaignAccess(
        campaign_id=row["campaign_public_id"],
        campaign_code=row["campaign_code"],
        campaign_name=row["campaign_name"],
        asset_kind=row["asset_kind"],
        access_level=row["access_level"],
        granted_at=row["granted_at"],
        granted_by=row.get("granted_by_public_id"),
    )


def _sqlstate(exc: Exception) -> str | None:
    return getattr(getattr(exc, "orig", None), "sqlstate", None)


@router.get("/users", response_model=DataEnvelope[list[UserView]])
def list_users(
    _: AdminActor,
    session: DbSession,
) -> DataEnvelope[list[UserView]]:
    return DataEnvelope(
        data=[_user(row) for row in SecurityRepository(session).list_users()]
    )


@router.post(
    "/users",
    response_model=DataEnvelope[UserView],
    status_code=status.HTTP_201_CREATED,
)
def create_user(
    payload: UserCreateRequest,
    actor: AdminActor,
    session: DbSession,
) -> DataEnvelope[UserView]:
    repository = SecurityRepository(session)
    try:
        user_id = repository.create_user(
            username=payload.username,
            display_name=payload.display_name,
            role=payload.role,
            password_hash=AuthService(
                get_settings(), repository
            ).hash_new_password(payload.password),
            must_change_password=payload.must_change_password,
            request_id=actor.request_id,
            source_ip=actor.source_ip,
        )
    except IntegrityError as exc:
        if _sqlstate(exc) == "23505":
            raise ApiError(
                409, "conflict", "用户名已存在。"
            ) from exc
        raise
    row = next(
        item
        for item in repository.list_users()
        if UUID(str(item["public_id"])) == user_id
    )
    return DataEnvelope(data=_user(row))


@router.post(
    "/service-principals",
    response_model=DataEnvelope[ServiceCredentialGrant],
    status_code=status.HTTP_201_CREATED,
)
def provision_service_principal(
    payload: ServiceCredentialCreateRequest,
    actor: AdminActor,
    session: DbSession,
) -> DataEnvelope[ServiceCredentialGrant]:
    service = ServiceCredentialService(
        get_settings(), SecurityRepository(session)
    )
    try:
        grant = service.provision(
            username=payload.username,
            display_name=payload.display_name,
            source_code=payload.source_code,
            campaign_ids=payload.campaign_ids,
            expires_at=payload.expires_at,
            request_id=actor.request_id,
            source_ip=actor.source_ip,
        )
    except IntegrityError as exc:
        if _sqlstate(exc) == "23505":
            raise ApiError(
                409,
                "conflict",
                "服务主体用户名、source_code或key_id冲突。",
            ) from exc
        raise
    return DataEnvelope(data=ServiceCredentialGrant(**grant.__dict__))


@router.post(
    "/service-principals/{service_id}/rotate",
    response_model=DataEnvelope[ServiceCredentialGrant],
)
def rotate_service_credential(
    service_id: UUID,
    payload: ServiceCredentialRotateRequest,
    actor: AdminActor,
    session: DbSession,
) -> DataEnvelope[ServiceCredentialGrant]:
    try:
        grant = ServiceCredentialService(
            get_settings(), SecurityRepository(session)
        ).rotate(
            service_id=service_id,
            revoke_previous=payload.revoke_previous,
            expires_at=payload.expires_at,
            reason=payload.reason,
            request_id=actor.request_id,
            source_ip=actor.source_ip,
        )
    except Exception as exc:
        if _sqlstate(exc) == "P0002":
            raise ApiError(404, "not_found", "服务主体不存在。") from exc
        raise
    return DataEnvelope(data=ServiceCredentialGrant(**grant.__dict__))


@router.post(
    "/service-principals/{service_id}/revoke",
    response_model=DataEnvelope[ServiceCredentialRevocation],
)
def revoke_service_credentials(
    service_id: UUID,
    payload: ServiceCredentialRevokeRequest,
    actor: AdminActor,
    session: DbSession,
) -> DataEnvelope[ServiceCredentialRevocation]:
    try:
        count = SecurityRepository(session).revoke_service_credentials(
            service_id=service_id,
            disable_principal=payload.disable_principal,
            reason=payload.reason,
            request_id=actor.request_id,
            source_ip=actor.source_ip,
        )
    except Exception as exc:
        if _sqlstate(exc) == "P0002":
            raise ApiError(404, "not_found", "服务主体不存在。") from exc
        raise
    return DataEnvelope(
        data=ServiceCredentialRevocation(
            service_id=service_id,
            revoked_count=count,
            principal_disabled=payload.disable_principal,
        )
    )


@router.post(
    "/users/{user_id}/disable",
    response_model=DataEnvelope[UserView],
)
def disable_user(
    user_id: UUID,
    payload: ReasonRequest,
    actor: AdminActor,
    session: DbSession,
) -> DataEnvelope[UserView]:
    repository = SecurityRepository(session)
    try:
        disabled = repository.disable_user(
            user_id, payload.reason, actor.request_id, actor.source_ip
        )
    except Exception as exc:
        if _sqlstate(exc) == "42501":
            raise ApiError(
                403,
                "forbidden",
                "管理员不能停用自己的账户。",
            ) from exc
        raise
    if not disabled:
        raise ApiError(404, "not_found", "账户不存在。")
    row = next(
        item
        for item in repository.list_users()
        if UUID(str(item["public_id"])) == user_id
    )
    return DataEnvelope(data=_user(row))


@router.get(
    "/access-requests",
    response_model=DataEnvelope[list[AccessRequest]],
)
def list_access_requests(
    _: AdminActor,
    session: DbSession,
    status_filter: RequestStatus = "PENDING",
) -> DataEnvelope[list[AccessRequest]]:
    return DataEnvelope(
        data=[
            _access_request(row)
            for row in SecurityRepository(session).admin_access_requests(
                status_filter
            )
        ]
    )


def _decide(
    access_request_id: UUID,
    payload: AccessDecision,
    actor: RequestActor,
    session: DbSession,
    decision: Literal["APPROVED", "REJECTED"],
) -> DataEnvelope[AccessRequest]:
    repository = SecurityRepository(session)
    try:
        changed = repository.decide_access_request(
            access_request_id,
            decision,
            payload.reason,
            actor.request_id,
            actor.source_ip,
        )
    except Exception as exc:
        if _sqlstate(exc) == "40001":
            raise ApiError(
                409, "conflict", "访问申请已被处理。"
            ) from exc
        if _sqlstate(exc) == "42501":
            raise ApiError(
                403, "forbidden", "管理员不能审批自己的访问申请。"
            ) from exc
        raise
    if not changed:
        raise ApiError(404, "not_found", "访问申请不存在。")
    row = next(
        item
        for item in repository.admin_access_requests(None)
        if UUID(str(item["public_id"])) == access_request_id
    )
    return DataEnvelope(data=_access_request(row))


@router.post(
    "/access-requests/{access_request_id}/approve",
    response_model=DataEnvelope[AccessRequest],
)
def approve_access_request(
    access_request_id: UUID,
    payload: AccessDecision,
    actor: AdminActor,
    session: DbSession,
) -> DataEnvelope[AccessRequest]:
    return _decide(access_request_id, payload, actor, session, "APPROVED")


@router.post(
    "/access-requests/{access_request_id}/reject",
    response_model=DataEnvelope[AccessRequest],
)
def reject_access_request(
    access_request_id: UUID,
    payload: AccessDecision,
    actor: AdminActor,
    session: DbSession,
) -> DataEnvelope[AccessRequest]:
    return _decide(access_request_id, payload, actor, session, "REJECTED")


@router.get(
    "/users/{user_id}/campaign-grants",
    response_model=DataEnvelope[list[CampaignAccess]],
)
def list_user_campaign_grants(
    user_id: UUID,
    _: AdminActor,
    session: DbSession,
) -> DataEnvelope[list[CampaignAccess]]:
    return DataEnvelope(
        data=[
            _grant(row)
            for row in SecurityRepository(session).list_user_grants(user_id)
        ]
    )


@router.post(
    "/users/{user_id}/campaign-grants",
    response_model=DataEnvelope[CampaignAccess],
)
def set_user_campaign_grant(
    user_id: UUID,
    payload: CampaignGrantRequest,
    actor: AdminActor,
    session: DbSession,
) -> DataEnvelope[CampaignAccess]:
    repository = SecurityRepository(session)
    if not repository.set_user_grant(
        user_id,
        payload.campaign_id,
        payload.access_level,
        payload.reason,
        actor.request_id,
        actor.source_ip,
    ):
        raise ApiError(404, "not_found", "账户或Campaign不存在。")
    row = next(
        item
        for item in repository.list_user_grants(user_id)
        if UUID(str(item["campaign_public_id"])) == payload.campaign_id
    )
    return DataEnvelope(data=_grant(row))


@router.delete(
    "/users/{user_id}/campaign-grants/{campaign_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
def revoke_user_campaign_grant(
    user_id: UUID,
    campaign_id: UUID,
    payload: ReasonRequest,
    actor: AdminActor,
    session: DbSession,
) -> Response:
    if not SecurityRepository(session).revoke_user_grant(
        user_id,
        campaign_id,
        payload.reason,
        actor.request_id,
        actor.source_ip,
    ):
        raise ApiError(404, "not_found", "Campaign授权不存在。")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/audit/security",
    response_model=DataEnvelope[list[AuditEvent]],
)
def list_security_audit(
    _: AdminActor,
    session: DbSession,
    limit: Annotated[int, Query(ge=1, le=200)] = 200,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> DataEnvelope[list[AuditEvent]]:
    events = [
        AuditEvent(
            id=row["public_id"],
            event_type=row["event_type"],
            actor_id=row["actor_public_id"],
            actor_username=row["actor_username"],
            username=row["username"],
            outcome=row["outcome"],
            request_id=row["request_id"],
            source_ip=row["source_ip"],
            metadata=row["metadata"],
            created_at=row["created_at"],
        )
        for row in SecurityRepository(session).list_security_events(limit, offset)
    ]
    return DataEnvelope(data=events)
