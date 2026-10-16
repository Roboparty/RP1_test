from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

import app.api.routes.auth as auth_routes
import app.core.auth as auth_module
import app.core.db as db_module
from app.core.auth import RequestActor, RequestActorDep, require_roles
from app.core.config import Settings
from app.core.db import get_public_db
from app.core.errors import ApiError
from app.schemas.security import Principal
from app.services.auth import (
    AuthService,
    ServiceCredentialService,
    SessionGrant,
)


class FakeSession:
    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def commit(self) -> None:
        return None

    def rollback(self) -> None:
        return None


def _api_error_app() -> FastAPI:
    app = FastAPI()

    @app.exception_handler(ApiError)
    async def api_error_handler(_: Request, exc: ApiError):
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": {
                    "code": exc.code,
                    "message": exc.message,
                    "details": exc.details,
                }
            },
        )

    return app


def _principal(*, must_change_password: bool = False) -> Principal:
    return Principal(
        id=uuid4(),
        username="viewer",
        display_name="Viewer",
        role="VIEWER",
        kind="HUMAN",
        session_id=uuid4(),
        session_type="BROWSER",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=8),
        must_change_password=must_change_password,
        csrf_token_hash=AuthService.secret_hash("csrf-token"),
    )


def test_http_auth_gate_requires_session(monkeypatch):
    monkeypatch.setattr(
        auth_module, "get_settings", lambda: Settings(auth_mode="session")
    )
    app = _api_error_app()

    @app.get("/protected")
    def protected(_: RequestActorDep):
        return {"ok": True}

    response = TestClient(app).get("/protected")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "authentication_required"


def test_service_bearer_is_service_only_and_ingestion_only(monkeypatch):
    settings = Settings(auth_mode="session")
    monkeypatch.setattr(auth_module, "get_settings", lambda: settings)
    monkeypatch.setattr(db_module, "SessionLocal", FakeSession)
    service_principal = Principal(
        id=uuid4(),
        username="collector",
        display_name="Collector",
        role="TEST_EXECUTOR",
        kind="SERVICE",
        session_id=None,
        session_type="SERVICE_API",
        expires_at=None,
        must_change_password=False,
        credential_id=uuid4(),
        source_id=uuid4(),
        source_code="EDGE-COLLECTOR",
    )
    monkeypatch.setattr(
        ServiceCredentialService,
        "resolve",
        lambda self, token: (
            service_principal if token == "rp1svc_key_secret" else None
        ),
    )
    app = _api_error_app()

    @app.post("/api/v1/ingestion/probe")
    def ingestion_probe(actor: RequestActorDep):
        return {
            "kind": actor.principal_kind,
            "session_type": actor.session_type,
        }

    @app.get("/api/v1/assets")
    def read_probe(_: RequestActorDep):
        return {"ok": True}

    accepted = TestClient(app).post(
        "/api/v1/ingestion/probe",
        headers={"Authorization": "Bearer rp1svc_key_secret"},
    )
    assert accepted.status_code == 200
    assert accepted.json() == {
        "kind": "SERVICE",
        "session_type": "SERVICE_API",
    }

    denied = TestClient(app).get(
        "/api/v1/assets",
        headers={"Authorization": "Bearer rp1svc_key_secret"},
    )
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "bearer_not_allowed"


def test_browser_session_requires_csrf_and_forced_password_change(monkeypatch):
    settings = Settings(auth_mode="session")
    monkeypatch.setattr(auth_module, "get_settings", lambda: settings)
    monkeypatch.setattr(db_module, "SessionLocal", FakeSession)
    principal = _principal()
    monkeypatch.setattr(AuthService, "resolve", lambda self, token: principal)
    app = _api_error_app()

    @app.post("/protected")
    def protected(_: RequestActorDep):
        return {"ok": True}

    client = TestClient(app)
    client.cookies.set(settings.auth_cookie_name, "opaque-token")
    missing = client.post("/protected")
    assert missing.status_code == 403
    assert missing.json()["error"]["code"] == "csrf_invalid"
    client.cookies.set(settings.auth_csrf_cookie_name, "different-cookie-token")
    mismatched = client.post(
        "/protected", headers={"X-CSRF-Token": "csrf-token"}
    )
    assert mismatched.status_code == 403
    client.cookies.set(settings.auth_csrf_cookie_name, "csrf-token")
    accepted = client.post(
        "/protected", headers={"X-CSRF-Token": "csrf-token"}
    )
    assert accepted.status_code == 200

    monkeypatch.setattr(
        AuthService,
        "resolve",
        lambda self, token: _principal(must_change_password=True),
    )
    forced = client.post(
        "/protected", headers={"X-CSRF-Token": "csrf-token"}
    )
    assert forced.status_code == 403
    assert forced.json()["error"]["code"] == "password_change_required"


def test_viewer_cannot_enter_admin_route():
    app = _api_error_app()
    viewer = RequestActor(
        public_id=uuid4(),
        request_id="viewer-admin",
        change_reason="",
        source_ip="127.0.0.1",
        username="viewer",
        display_name="Viewer",
        role="VIEWER",
        principal_kind="HUMAN",
        session_public_id=uuid4(),
        session_type="BROWSER",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=8),
        must_change_password=False,
    )

    @app.get("/admin")
    def admin_only(
        _: RequestActor = Depends(require_roles("SYSTEM_ADMIN")),
    ):
        return {"ok": True}

    app.dependency_overrides[auth_module.get_request_actor] = lambda: viewer
    response = TestClient(app).get("/admin")
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"


def test_login_sets_strict_http_only_session_and_csrf_cookies(monkeypatch):
    settings = Settings(
        auth_mode="session",
        app_env="test",
        auth_cookie_secure=False,
    )
    principal = _principal()
    expires_at = datetime.now(timezone.utc) + timedelta(hours=8)
    grant = SessionGrant(
        token="opaque-session",
        csrf_token="csrf-token",
        expires_at=expires_at,
        principal=principal,
    )
    monkeypatch.setattr(auth_routes, "get_settings", lambda: settings)
    monkeypatch.setattr(AuthService, "login", lambda self, *args: grant)
    app = _api_error_app()
    app.include_router(auth_routes.router)
    app.dependency_overrides[get_public_db] = FakeSession

    response = TestClient(app).post(
        "/api/v1/auth/login",
        json={"username": "viewer", "password": "correct-password"},
    )
    assert response.status_code == 200
    cookies = response.headers.get_list("set-cookie")
    session_cookie = next(
        item for item in cookies if item.startswith(f"{settings.auth_cookie_name}=")
    )
    csrf_cookie = next(
        item
        for item in cookies
        if item.startswith(f"{settings.auth_csrf_cookie_name}=")
    )
    assert "HttpOnly" in session_cookie
    assert "SameSite=strict" in session_cookie
    assert "HttpOnly" not in csrf_cookie
    assert "SameSite=strict" in csrf_cookie
