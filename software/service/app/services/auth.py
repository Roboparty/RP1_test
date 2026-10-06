from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import UUID

from argon2 import PasswordHasher, Type
from argon2.exceptions import InvalidHashError, VerificationError

from app.core.config import Settings
from app.repositories.security import SecurityRepository
from app.schemas.security import Principal, UserView


DUMMY_PASSWORD_HASH = (
    "$argon2id$v=19$m=65536,t=3,p=4$NhwzPN6p+i8wvSq/f4V4vQ$"
    "2JszfwV08Ei03zxCvTuwUnEmLxNboDtgfF25UL1JKJA"
)


class InvalidCredentialsError(Exception):
    pass


class CurrentPasswordInvalidError(Exception):
    pass


@dataclass(frozen=True)
class SessionGrant:
    token: str
    csrf_token: str
    expires_at: datetime
    principal: Principal


class AuthService:
    def __init__(
        self,
        settings: Settings,
        repository: SecurityRepository,
        password_hasher: PasswordHasher | None = None,
    ) -> None:
        self.settings = settings
        self.repository = repository
        self.password_hasher = password_hasher or PasswordHasher(type=Type.ID)

    @staticmethod
    def secret_hash(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    def bootstrap_initial_admin(
        self,
        request_id: str = "application-startup",
        source_ip: str | None = None,
    ) -> bool:
        username = self.settings.seed_admin_username
        configured_password = self.settings.seed_admin_password
        if not username or configured_password is None:
            return False
        password = configured_password.get_secret_value()
        try:
            created = self.repository.bootstrap_initial_admin(
                username=username,
                display_name=self.settings.seed_admin_display_name,
                password_hash=self.password_hasher.hash(password),
                request_id=request_id,
                source_ip=source_ip,
            )
            self.repository.session.commit()
            return created
        except Exception:
            self.repository.session.rollback()
            raise

    def login(
        self,
        username: str,
        password: str,
        request_id: str,
        source_ip: str | None,
    ) -> SessionGrant:
        normalized = username.strip().lower()
        row = self.repository.lookup_login_credential(normalized)
        candidate_hash = str(row["password_hash"]) if row else DUMMY_PASSWORD_HASH
        valid = self._verify_password(candidate_hash, password)
        now = datetime.now(timezone.utc)
        blocked = (
            row is None
            or not bool(row["enabled"])
            or row["principal_kind"] != "HUMAN"
            or (
                row["locked_until"] is not None
                and self._as_aware(row["locked_until"]) > now
            )
        )
        if not valid or blocked:
            self.repository.record_login_failure(normalized, request_id, source_ip)
            self.repository.session.commit()
            raise InvalidCredentialsError

        assert row is not None
        user_id = UUID(str(row["user_public_id"]))
        rehash = (
            self.password_hasher.hash(password)
            if self.password_hasher.check_needs_rehash(candidate_hash)
            else None
        )
        token = secrets.token_urlsafe(48)
        csrf_token = secrets.token_urlsafe(48)
        expires_at = now + timedelta(hours=self.settings.auth_browser_session_hours)
        try:
            self.repository.record_login_success(
                user_id, request_id, source_ip, rehash
            )
            session_row = self.repository.create_browser_session(
                user_id,
                self.secret_hash(token),
                self.secret_hash(csrf_token),
                expires_at,
            )
            self.repository.session.commit()
        except Exception:
            self.repository.session.rollback()
            self.repository.record_login_failure(normalized, request_id, source_ip)
            self.repository.session.commit()
            raise InvalidCredentialsError from None

        principal = Principal(
            id=user_id,
            username=str(row["normalized_username"]),
            display_name=str(row["display_name"]),
            role=row["role"],
            kind=row["principal_kind"],
            session_id=UUID(str(session_row["session_public_id"])),
            session_type="BROWSER",
            expires_at=self._as_aware(session_row["expires_at"]),
            must_change_password=bool(row["must_change_password"]),
            csrf_token_hash=self.secret_hash(csrf_token),
        )
        return SessionGrant(
            token=token,
            csrf_token=csrf_token,
            expires_at=expires_at,
            principal=principal,
        )

    def resolve(self, token: str) -> Principal | None:
        if not token:
            return None
        row = self.repository.resolve_browser_session(self.secret_hash(token))
        if row is None:
            return None
        return Principal(
            id=UUID(str(row["user_public_id"])),
            username=str(row["username"]),
            display_name=str(row["display_name"]),
            role=row["role"],
            kind=row["principal_kind"],
            session_id=UUID(str(row["session_public_id"])),
            session_type="BROWSER",
            expires_at=self._as_aware(row["expires_at"]),
            must_change_password=bool(row["must_change_password"]),
            csrf_token_hash=str(row["csrf_token_hash"]),
        )

    @staticmethod
    def verify_csrf(principal: Principal, token: str) -> bool:
        if principal.session_type != "BROWSER":
            return True
        if not token or not principal.csrf_token_hash:
            return False
        return hmac.compare_digest(
            AuthService.secret_hash(token), principal.csrf_token_hash
        )

    def change_password(
        self,
        principal: Principal,
        current_password: str,
        new_password: str,
        request_id: str,
        source_ip: str | None,
    ) -> UserView:
        row = self.repository.lookup_login_credential(principal.username)
        if (
            row is None
            or UUID(str(row["user_public_id"])) != principal.id
            or not self._verify_password(str(row["password_hash"]), current_password)
        ):
            raise CurrentPasswordInvalidError
        if len(new_password) < 12:
            raise ValueError("new password must contain at least 12 characters")
        if current_password == new_password:
            raise ValueError("new password must differ from current password")
        if principal.session_id is None:
            raise ValueError("browser session is required")
        self.repository.change_current_password(
            principal.session_id,
            self.password_hasher.hash(new_password),
            request_id,
            source_ip,
        )
        user_data = principal.user_view().model_dump()
        user_data["must_change_password"] = False
        return UserView(**user_data)

    def hash_new_password(self, password: str) -> str:
        if len(password) < 12:
            raise ValueError("password must contain at least 12 characters")
        return self.password_hasher.hash(password)

    def _verify_password(self, password_hash: str, password: str) -> bool:
        try:
            return bool(self.password_hasher.verify(password_hash, password))
        except (VerificationError, InvalidHashError):
            return False

    @staticmethod
    def _as_aware(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value


@dataclass(frozen=True)
class ServiceCredentialGrant:
    service_id: UUID
    credential_id: UUID
    source_id: UUID | None
    api_key: str
    key_id: str
    expires_at: datetime | None


class ServiceCredentialService:
    KEY_PREFIX = "rp1svc"

    def __init__(
        self, settings: Settings, repository: SecurityRepository
    ) -> None:
        self.settings = settings
        self.repository = repository

    def resolve(self, api_key: str) -> Principal | None:
        parsed = self._parse_key(api_key)
        if parsed is None:
            return None
        key_id, secret = parsed
        row = self.repository.resolve_service_credential(
            key_id, self._secret_hash(secret)
        )
        if row is None:
            return None
        return Principal(
            id=UUID(str(row["user_public_id"])),
            username=str(row["username"]),
            display_name=str(row["display_name"]),
            role=row["role"],
            kind=row["principal_kind"],
            session_id=None,
            session_type="SERVICE_API",
            expires_at=None,
            must_change_password=False,
            credential_id=UUID(str(row["credential_public_id"])),
            source_id=UUID(str(row["source_public_id"])),
            source_code=str(row["source_code"]),
        )

    def provision(
        self,
        *,
        username: str,
        display_name: str,
        source_code: str,
        campaign_ids: list[UUID],
        expires_at: datetime | None,
        request_id: str,
        source_ip: str | None,
    ) -> ServiceCredentialGrant:
        key_id, secret, api_key = self._new_key()
        row = self.repository.provision_service_credential(
            username=username,
            display_name=display_name,
            source_code=source_code,
            key_id=key_id,
            secret_hash=self._secret_hash(secret),
            campaign_ids=campaign_ids,
            expires_at=expires_at,
            request_id=request_id,
            source_ip=source_ip,
        )
        return ServiceCredentialGrant(
            service_id=UUID(str(row["service_public_id"])),
            credential_id=UUID(str(row["credential_public_id"])),
            source_id=UUID(str(row["source_public_id"])),
            api_key=api_key,
            key_id=key_id,
            expires_at=expires_at,
        )

    def rotate(
        self,
        *,
        service_id: UUID,
        revoke_previous: bool,
        expires_at: datetime | None,
        reason: str,
        request_id: str,
        source_ip: str | None,
    ) -> ServiceCredentialGrant:
        key_id, secret, api_key = self._new_key()
        credential_id = self.repository.rotate_service_credential(
            service_id=service_id,
            key_id=key_id,
            secret_hash=self._secret_hash(secret),
            revoke_previous=revoke_previous,
            expires_at=expires_at,
            reason=reason,
            request_id=request_id,
            source_ip=source_ip,
        )
        return ServiceCredentialGrant(
            service_id=service_id,
            credential_id=credential_id,
            source_id=None,
            api_key=api_key,
            key_id=key_id,
            expires_at=expires_at,
        )

    def _secret_hash(self, secret: str) -> str:
        return hmac.new(
            self.settings.service_api_key_hmac_secret.get_secret_value().encode(
                "utf-8"
            ),
            secret.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    @classmethod
    def _new_key(cls) -> tuple[str, str, str]:
        key_id = secrets.token_hex(12)
        secret = secrets.token_urlsafe(32)
        return key_id, secret, f"{cls.KEY_PREFIX}_{key_id}_{secret}"

    @classmethod
    def _parse_key(cls, api_key: str) -> tuple[str, str] | None:
        parts = api_key.split("_", 2)
        if (
            len(parts) != 3
            or parts[0] != cls.KEY_PREFIX
            or not 12 <= len(parts[1]) <= 64
            or len(parts[2]) < 32
        ):
            return None
        return parts[1], parts[2]
