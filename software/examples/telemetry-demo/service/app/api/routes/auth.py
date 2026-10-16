from __future__ import annotations

from fastapi import APIRouter, Request, Response, status

from app.core.auth import RequestActorDep
from app.core.config import get_settings
from app.core.db import DbSession, PublicDbSession
from app.core.errors import ApiError
from app.repositories.security import SecurityRepository
from app.schemas.common import DataEnvelope
from app.schemas.security import (
    CampaignAccess,
    ChangePasswordRequest,
    LoginRequest,
    LoginResponse,
    MeResponse,
    Principal,
    UserView,
)
from app.services.auth import (
    AuthService,
    CurrentPasswordInvalidError,
    InvalidCredentialsError,
)


router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


def _user_from_actor(actor: RequestActorDep) -> UserView:
    return UserView(
        id=actor.public_id,
        username=actor.username,
        display_name=actor.display_name,
        role=actor.role,
        kind=actor.principal_kind,
        enabled=True,
        must_change_password=actor.must_change_password,
    )


def _campaign_access(row: dict) -> CampaignAccess:
    return CampaignAccess(
        campaign_id=row["campaign_public_id"],
        campaign_code=row["campaign_code"],
        campaign_name=row["campaign_name"],
        asset_kind=row["asset_kind"],
        access_level=row["access_level"],
        granted_at=row["granted_at"],
        granted_by=row.get("granted_by_public_id"),
    )


@router.post("/login", response_model=DataEnvelope[LoginResponse])
def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    session: PublicDbSession,
) -> DataEnvelope[LoginResponse]:
    settings = get_settings()
    service = AuthService(settings, SecurityRepository(session))
    try:
        grant = service.login(
            payload.username,
            payload.password,
            str(getattr(request.state, "request_id", "")),
            request.client.host if request.client else None,
        )
    except InvalidCredentialsError as exc:
        raise ApiError(
            401,
            "invalid_credentials",
            "用户名或密码错误。",
        ) from exc

    max_age = settings.auth_browser_session_hours * 3600
    response.set_cookie(
        settings.auth_cookie_name,
        grant.token,
        max_age=max_age,
        httponly=True,
        secure=settings.secure_auth_cookie,
        samesite="strict",
        path="/",
    )
    response.set_cookie(
        settings.auth_csrf_cookie_name,
        grant.csrf_token,
        max_age=max_age,
        httponly=False,
        secure=settings.secure_auth_cookie,
        samesite="strict",
        path="/",
    )
    return DataEnvelope(
        data=LoginResponse(
            user=grant.principal.user_view(),
            expires_at=grant.expires_at,
        )
    )


@router.get("/me", response_model=DataEnvelope[MeResponse])
def me(actor: RequestActorDep, session: DbSession) -> DataEnvelope[MeResponse]:
    access = [
        _campaign_access(row)
        for row in SecurityRepository(session).current_campaign_access()
    ]
    return DataEnvelope(
        data=MeResponse(
            user=_user_from_actor(actor),
            campaign_access=access,
            session_type=actor.session_type,
            expires_at=actor.expires_at,
        )
    )


@router.post(
    "/change-password",
    response_model=DataEnvelope[UserView],
)
def change_password(
    payload: ChangePasswordRequest,
    actor: RequestActorDep,
    session: DbSession,
) -> DataEnvelope[UserView]:
    service = AuthService(get_settings(), SecurityRepository(session))
    try:
        user = service.change_password(
            principal=_principal_from_actor(actor),
            current_password=payload.current_password,
            new_password=payload.new_password,
            request_id=actor.request_id,
            source_ip=actor.source_ip,
        )
    except CurrentPasswordInvalidError as exc:
        raise ApiError(
            401,
            "invalid_credentials",
            "当前密码错误。",
        ) from exc
    except ValueError as exc:
        raise ApiError(422, "validation_error", str(exc)) from exc
    return DataEnvelope(data=user)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(
    response: Response,
    actor: RequestActorDep,
    session: DbSession,
) -> Response:
    settings = get_settings()
    if actor.session_public_id is not None:
        SecurityRepository(session).revoke_browser_session(
            actor.session_public_id,
            actor.request_id,
            actor.source_ip,
        )
    response.delete_cookie(settings.auth_cookie_name, path="/")
    response.delete_cookie(settings.auth_csrf_cookie_name, path="/")
    response.status_code = status.HTTP_204_NO_CONTENT
    return response


def _principal_from_actor(actor: RequestActorDep) -> Principal:
    return Principal(
        id=actor.public_id,
        username=actor.username,
        display_name=actor.display_name,
        role=actor.role,
        kind=actor.principal_kind,
        session_id=actor.session_public_id,
        session_type=actor.session_type,
        expires_at=actor.expires_at,
        must_change_password=actor.must_change_password,
    )
