from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
import hmac
from typing import Annotated
from uuid import UUID

from fastapi import Depends, Header, Request, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.config import get_settings
from app.core.errors import ApiError
from app.schemas.security import Role


SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
service_bearer = HTTPBearer(
    auto_error=False,
    scheme_name="ServiceBearer",
    description="SERVICE principal API key; accepted only by /api/v1/ingestion.",
)
PASSWORD_CHANGE_ALLOWED_PATHS = {
    "/api/v1/auth/me",
    "/api/v1/auth/change-password",
    "/api/v1/auth/logout",
}


@dataclass(frozen=True)
class RequestActor:
    public_id: UUID
    request_id: str
    change_reason: str
    source_ip: str | None
    username: str
    display_name: str
    role: Role
    principal_kind: str
    session_public_id: UUID | None
    session_type: str
    expires_at: datetime | None
    must_change_password: bool
    credential_public_id: UUID | None = None
    source_public_id: UUID | None = None
    source_code: str | None = None


def get_request_actor(
    request: Request,
    x_user_public_id: Annotated[str | None, Header(alias="X-User-Public-Id")] = None,
    x_request_id: Annotated[str | None, Header(alias="X-Request-Id")] = None,
    x_change_reason: Annotated[str | None, Header(alias="X-Change-Reason")] = None,
) -> RequestActor:
    settings = get_settings()
    request_id = x_request_id or getattr(request.state, "request_id", "api-request")
    source_ip = request.client.host if request.client else None
    authorization = request.headers.get("Authorization", "")

    if authorization:
        if not request.url.path.startswith("/api/v1/ingestion"):
            raise ApiError(
                403,
                "bearer_not_allowed",
                "Bearer服务凭据仅可用于ingestion接口。",
            )
        scheme, separator, token = authorization.partition(" ")
        if separator != " " or scheme.lower() != "bearer" or not token:
            raise ApiError(
                401, "authentication_required", "Bearer服务凭据格式无效。"
            )
        from app.core.db import SessionLocal
        from app.repositories.security import SecurityRepository
        from app.services.auth import ServiceCredentialService

        with SessionLocal() as session:
            principal = ServiceCredentialService(
                settings, SecurityRepository(session)
            ).resolve(token)
            session.commit()
        if principal is None or principal.kind != "SERVICE":
            raise ApiError(
                401, "authentication_required", "服务凭据无效或已吊销。"
            )
        return RequestActor(
            public_id=principal.id,
            request_id=request_id,
            change_reason=x_change_reason or "edge collector ingestion",
            source_ip=source_ip,
            username=principal.username,
            display_name=principal.display_name,
            role=principal.role,
            principal_kind=principal.kind,
            session_public_id=None,
            session_type="SERVICE_API",
            expires_at=None,
            must_change_password=False,
            credential_public_id=principal.credential_id,
            source_public_id=principal.source_id,
            source_code=principal.source_code,
        )

    if settings.auth_mode == "development":
        raw_user_id = x_user_public_id or settings.dev_user_public_id
        try:
            user_id = UUID(raw_user_id)
        except ValueError as exc:
            raise ApiError(
                401, "authentication_required", "开发身份不是有效UUID。"
            ) from exc
        return RequestActor(
            public_id=user_id,
            request_id=request_id,
            change_reason=x_change_reason or "",
            source_ip=source_ip,
            username="development",
            display_name="Development user",
            role="SYSTEM_ADMIN",
            principal_kind="HUMAN",
            session_public_id=None,
            session_type="DEVELOPMENT",
            expires_at=None,
            must_change_password=False,
        )

    token = request.cookies.get(settings.auth_cookie_name, "")
    if not token:
        raise ApiError(401, "authentication_required", "需要登录后访问。")

    from app.core.db import SessionLocal
    from app.repositories.security import SecurityRepository
    from app.services.auth import AuthService

    with SessionLocal() as session:
        principal = AuthService(
            settings, SecurityRepository(session)
        ).resolve(token)
        session.commit()
    if principal is None:
        raise ApiError(401, "authentication_required", "会话无效或已过期。")
    if request.method.upper() not in SAFE_METHODS:
        csrf_header = request.headers.get("X-CSRF-Token", "")
        csrf_cookie = request.cookies.get(settings.auth_csrf_cookie_name, "")
        if (
            not csrf_header
            or not csrf_cookie
            or not hmac.compare_digest(csrf_header, csrf_cookie)
            or not AuthService.verify_csrf(principal, csrf_header)
        ):
            raise ApiError(403, "csrf_invalid", "CSRF令牌缺失或无效。")
    if (
        principal.must_change_password
        and request.url.path not in PASSWORD_CHANGE_ALLOWED_PATHS
    ):
        raise ApiError(
            403,
            "password_change_required",
            "必须修改初始密码后才能访问该接口。",
        )
    return RequestActor(
        public_id=principal.id,
        request_id=request_id,
        change_reason=x_change_reason or "",
        source_ip=source_ip,
        username=principal.username,
        display_name=principal.display_name,
        role=principal.role,
        principal_kind=principal.kind,
        session_public_id=principal.session_id,
        session_type=principal.session_type,
        expires_at=principal.expires_at,
        must_change_password=principal.must_change_password,
    )


def require_roles(*roles: Role) -> Callable[..., RequestActor]:
    def dependency(actor: RequestActorDep) -> RequestActor:
        if actor.role not in roles:
            raise ApiError(403, "forbidden", "当前账户没有执行该操作的角色权限。")
        return actor

    return dependency


def require_ingestion_service(
    actor: RequestActorDep,
    _: Annotated[
        HTTPAuthorizationCredentials | None,
        Security(service_bearer),
    ] = None,
) -> RequestActor:
    if (
        actor.principal_kind != "SERVICE"
        or actor.session_type != "SERVICE_API"
        or actor.source_public_id is None
    ):
        raise ApiError(
            403,
            "service_principal_required",
            "ingestion接口仅接受已授权的SERVICE Bearer凭据。",
        )
    return actor


RequestActorDep = Annotated[RequestActor, Depends(get_request_actor)]
