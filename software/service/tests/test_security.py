from __future__ import annotations

from datetime import datetime, timezone
from datetime import timedelta
from uuid import uuid4

import pytest
from argon2 import PasswordHasher, Type
from starlette.requests import Request

import app.core.auth as auth_module
from app.core.auth import RequestActor, get_request_actor, require_roles
from app.core.config import Settings
from app.core.errors import ApiError
from app.schemas.security import Principal
from app.services.auth import (
    AuthService,
    InvalidCredentialsError,
)


class FakeSession:
    def __init__(self) -> None:
        self.commits = 0
        self.rollbacks = 0

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


class FakeRepository:
    def __init__(self, credential: dict | None) -> None:
        self.credential = credential
        self.session = FakeSession()
        self.failures: list[str] = []
        self.bootstrap_calls: list[dict] = []

    def lookup_login_credential(self, username: str):
        return self.credential

    def record_login_failure(
        self, username: str, request_id: str, source_ip: str | None
    ) -> None:
        self.failures.append(username)

    def bootstrap_initial_admin(self, **kwargs):
        self.bootstrap_calls.append(kwargs)
        return True


def test_secret_hash_is_sha256_and_not_plaintext():
    digest = AuthService.secret_hash("opaque-secret")
    assert len(digest) == 64
    assert digest == AuthService.secret_hash("opaque-secret")
    assert digest != "opaque-secret"


def test_new_password_requires_twelve_characters():
    service = AuthService(
        Settings(),
        FakeRepository(None),  # type: ignore[arg-type]
    )
    with pytest.raises(ValueError, match="12"):
        service.hash_new_password("short")
    assert service.hash_new_password("long-enough-password").startswith("$argon2id$")


def test_initial_admin_bootstrap_hashes_password_and_commits():
    repository = FakeRepository(None)
    service = AuthService(
        Settings(
            seed_admin_username="local-admin",
            seed_admin_password="RP1-Local-Admin-2026!",
        ),
        repository,  # type: ignore[arg-type]
    )
    assert service.bootstrap_initial_admin()
    assert repository.session.commits == 1
    call = repository.bootstrap_calls[0]
    assert call["username"] == "local-admin"
    assert call["password_hash"].startswith("$argon2id$")
    assert call["password_hash"] != "RP1-Local-Admin-2026!"


def test_admin_seed_configuration_must_be_paired_and_production_safe(monkeypatch):
    monkeypatch.delenv("SEED_ADMIN_PASSWORD", raising=False)
    with pytest.raises(ValueError, match="together"):
        Settings(seed_admin_username="local-admin")
    with pytest.raises(ValueError, match="production"):
        Settings(
            app_env="production",
            seed_admin_username="local-admin",
            seed_admin_password="RP1-Local-Admin-2026!",
        )


@pytest.mark.parametrize(
    "credential,password",
    [
        (None, "wrong-password"),
        (
            {
                "password_hash": PasswordHasher(type=Type.ID).hash(
                    "correct-password"
                ),
                "enabled": True,
                "principal_kind": "HUMAN",
                "locked_until": None,
            },
            "wrong-password",
        ),
    ],
)
def test_unknown_user_and_wrong_password_share_generic_failure(
    credential: dict | None, password: str
):
    repository = FakeRepository(credential)
    service = AuthService(
        Settings(),
        repository,  # type: ignore[arg-type]
    )
    with pytest.raises(InvalidCredentialsError):
        service.login(
            "Some.User",
            password,
            request_id="unit-login",
            source_ip="127.0.0.1",
        )
    assert repository.failures == ["some.user"]
    assert repository.session.commits == 1


@pytest.mark.parametrize(
    "overrides",
    [
        {"enabled": False},
        {"principal_kind": "SERVICE"},
        {"locked_until": datetime.now(timezone.utc) + timedelta(minutes=5)},
    ],
)
def test_disabled_service_and_locked_principals_cannot_login(overrides):
    password = "correct-password"
    credential = {
        "password_hash": PasswordHasher(type=Type.ID).hash(password),
        "enabled": True,
        "principal_kind": "HUMAN",
        "locked_until": None,
        **overrides,
    }
    repository = FakeRepository(credential)
    service = AuthService(Settings(), repository)  # type: ignore[arg-type]
    with pytest.raises(InvalidCredentialsError):
        service.login("blocked-user", password, "blocked-login", "127.0.0.1")
    assert repository.failures == ["blocked-user"]


def test_browser_csrf_uses_hashed_constant_time_comparison():
    csrf_token = "csrf-secret"
    principal = Principal(
        id=uuid4(),
        username="viewer",
        display_name="Viewer",
        role="VIEWER",
        kind="HUMAN",
        session_id=uuid4(),
        session_type="BROWSER",
        expires_at=datetime.now(timezone.utc),
        csrf_token_hash=AuthService.secret_hash(csrf_token),
    )
    assert AuthService.verify_csrf(principal, csrf_token)
    assert not AuthService.verify_csrf(principal, "")
    assert not AuthService.verify_csrf(principal, "different")


def test_development_identity_does_not_require_csrf():
    principal = Principal(
        id=uuid4(),
        username="development",
        display_name="Development",
        role="SYSTEM_ADMIN",
        kind="HUMAN",
        session_type="DEVELOPMENT",
    )
    assert AuthService.verify_csrf(principal, "")


def _request(method: str = "GET", path: str = "/api/v1/assets") -> Request:
    return Request(
        {
            "type": "http",
            "method": method,
            "path": path,
            "headers": [],
            "client": ("127.0.0.1", 12345),
            "query_string": b"",
            "scheme": "http",
            "server": ("testserver", 80),
        }
    )


def test_session_mode_without_cookie_is_401(monkeypatch):
    monkeypatch.setattr(
        auth_module,
        "get_settings",
        lambda: Settings(auth_mode="session"),
    )
    with pytest.raises(ApiError) as error:
        get_request_actor(_request(), None, None, None)
    assert error.value.status_code == 401
    assert error.value.code == "authentication_required"


def test_development_fallback_is_explicit_and_role_guard_rejects_viewer(monkeypatch):
    development_id = uuid4()
    monkeypatch.setattr(
        auth_module,
        "get_settings",
        lambda: Settings(
            auth_mode="development",
            dev_user_public_id=str(development_id),
        ),
    )
    actor = get_request_actor(_request(), None, "request-id", None)
    assert actor.public_id == development_id
    assert actor.session_type == "DEVELOPMENT"

    viewer = RequestActor(
        public_id=uuid4(),
        request_id="role-test",
        change_reason="",
        source_ip="127.0.0.1",
        username="viewer",
        display_name="Viewer",
        role="VIEWER",
        principal_kind="HUMAN",
        session_public_id=uuid4(),
        session_type="BROWSER",
        expires_at=datetime.now(timezone.utc),
        must_change_password=False,
    )
    with pytest.raises(ApiError) as forbidden:
        require_roles("SYSTEM_ADMIN")(viewer)
    assert forbidden.value.status_code == 403
    assert forbidden.value.code == "forbidden"
