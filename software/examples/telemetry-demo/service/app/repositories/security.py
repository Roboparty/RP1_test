from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session


class SecurityRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    @staticmethod
    def _one_dict(result: Any) -> dict[str, Any] | None:
        row = result.mappings().first()
        return dict(row) if row else None

    @staticmethod
    def _all_dicts(result: Any) -> list[dict[str, Any]]:
        return [dict(row) for row in result.mappings().all()]

    def lookup_login_credential(self, username: str) -> dict[str, Any] | None:
        return self._one_dict(
            self.session.execute(
                text("SELECT * FROM iam.auth_lookup_login_credential(:username)"),
                {"username": username},
            )
        )

    def bootstrap_initial_admin(
        self,
        username: str,
        display_name: str,
        password_hash: str,
        request_id: str,
        source_ip: str | None,
    ) -> bool:
        return bool(
            self.session.execute(
                text(
                    "SELECT iam.bootstrap_initial_admin("
                    ":username, :display_name, :password_hash, :request_id, "
                    "CAST(:source_ip AS inet))"
                ),
                {
                    "username": username,
                    "display_name": display_name,
                    "password_hash": password_hash,
                    "request_id": request_id,
                    "source_ip": source_ip,
                },
            ).scalar_one()
        )

    def record_login_failure(
        self, username: str, request_id: str, source_ip: str | None
    ) -> None:
        self.session.execute(
            text(
                "SELECT iam.auth_record_login_failure("
                ":username, :request_id, CAST(:source_ip AS inet))"
            ),
            {
                "username": username,
                "request_id": request_id,
                "source_ip": source_ip,
            },
        )

    def record_login_success(
        self,
        user_id: UUID,
        request_id: str,
        source_ip: str | None,
        rehash: str | None,
    ) -> None:
        self.session.execute(
            text(
                "SELECT iam.auth_record_login_success("
                ":user_id, :request_id, CAST(:source_ip AS inet), :rehash)"
            ),
            {
                "user_id": str(user_id),
                "request_id": request_id,
                "source_ip": source_ip,
                "rehash": rehash,
            },
        )

    def create_browser_session(
        self,
        user_id: UUID,
        token_hash: str,
        csrf_token_hash: str,
        expires_at: datetime,
    ) -> dict[str, Any]:
        row = self._one_dict(
            self.session.execute(
                text(
                    "SELECT * FROM iam.auth_create_browser_session("
                    ":user_id, :token_hash, :csrf_hash, :expires_at)"
                ),
                {
                    "user_id": str(user_id),
                    "token_hash": token_hash,
                    "csrf_hash": csrf_token_hash,
                    "expires_at": expires_at,
                },
            )
        )
        if row is None:
            raise RuntimeError("browser session was not created")
        return row

    def resolve_browser_session(self, token_hash: str) -> dict[str, Any] | None:
        return self._one_dict(
            self.session.execute(
                text("SELECT * FROM iam.auth_resolve_browser_session(:token_hash)"),
                {"token_hash": token_hash},
            )
        )

    def resolve_service_credential(
        self, key_id: str, secret_hash: str
    ) -> dict[str, Any] | None:
        return self._one_dict(
            self.session.execute(
                text(
                    "SELECT * FROM iam.auth_resolve_service_credential("
                    ":key_id, :secret_hash)"
                ),
                {"key_id": key_id, "secret_hash": secret_hash},
            )
        )

    def provision_service_credential(
        self,
        *,
        username: str,
        display_name: str,
        source_code: str,
        key_id: str,
        secret_hash: str,
        campaign_ids: list[UUID],
        expires_at: datetime | None,
        request_id: str,
        source_ip: str | None,
    ) -> dict[str, Any]:
        row = self._one_dict(
            self.session.execute(
                text(
                    "SELECT * FROM iam.admin_provision_service_credential("
                    ":username, :display_name, :source_code, :key_id, "
                    ":secret_hash, CAST(:campaign_ids AS uuid[]), :expires_at, "
                    ":request_id, CAST(:source_ip AS inet))"
                ),
                {
                    "username": username,
                    "display_name": display_name,
                    "source_code": source_code,
                    "key_id": key_id,
                    "secret_hash": secret_hash,
                    "campaign_ids": [str(value) for value in campaign_ids],
                    "expires_at": expires_at,
                    "request_id": request_id,
                    "source_ip": source_ip,
                },
            )
        )
        if row is None:
            raise RuntimeError("service credential was not provisioned")
        return row

    def rotate_service_credential(
        self,
        *,
        service_id: UUID,
        key_id: str,
        secret_hash: str,
        revoke_previous: bool,
        expires_at: datetime | None,
        reason: str,
        request_id: str,
        source_ip: str | None,
    ) -> UUID:
        value = self.session.execute(
            text(
                "SELECT iam.admin_rotate_service_credential("
                ":service_id, :key_id, :secret_hash, :revoke_previous, "
                ":expires_at, :reason, :request_id, CAST(:source_ip AS inet))"
            ),
            {
                "service_id": str(service_id),
                "key_id": key_id,
                "secret_hash": secret_hash,
                "revoke_previous": revoke_previous,
                "expires_at": expires_at,
                "reason": reason,
                "request_id": request_id,
                "source_ip": source_ip,
            },
        ).scalar_one()
        return UUID(str(value))

    def revoke_service_credentials(
        self,
        *,
        service_id: UUID,
        disable_principal: bool,
        reason: str,
        request_id: str,
        source_ip: str | None,
    ) -> int:
        return int(
            self.session.execute(
                text(
                    "SELECT iam.admin_revoke_service_credentials("
                    ":service_id, :disable_principal, :reason, :request_id, "
                    "CAST(:source_ip AS inet))"
                ),
                {
                    "service_id": str(service_id),
                    "disable_principal": disable_principal,
                    "reason": reason,
                    "request_id": request_id,
                    "source_ip": source_ip,
                },
            ).scalar_one()
        )

    def revoke_browser_session(
        self,
        session_id: UUID,
        request_id: str,
        source_ip: str | None,
    ) -> bool:
        return bool(
            self.session.execute(
                text(
                    "SELECT iam.auth_revoke_browser_session("
                    ":session_id, :request_id, CAST(:source_ip AS inet))"
                ),
                {
                    "session_id": str(session_id),
                    "request_id": request_id,
                    "source_ip": source_ip,
                },
            ).scalar_one()
        )

    def change_current_password(
        self,
        session_id: UUID,
        password_hash: str,
        request_id: str,
        source_ip: str | None,
    ) -> None:
        self.session.execute(
            text(
                "SELECT iam.auth_change_current_password("
                ":session_id, :password_hash, :request_id, CAST(:source_ip AS inet))"
            ),
            {
                "session_id": str(session_id),
                "password_hash": password_hash,
                "request_id": request_id,
                "source_ip": source_ip,
            },
        )

    def list_users(self) -> list[dict[str, Any]]:
        return self._all_dicts(
            self.session.execute(text("SELECT * FROM iam.admin_list_users()"))
        )

    def create_user(
        self,
        *,
        username: str,
        display_name: str,
        role: str,
        password_hash: str,
        must_change_password: bool,
        request_id: str,
        source_ip: str | None,
    ) -> UUID:
        value = self.session.execute(
            text(
                "SELECT iam.admin_create_user("
                ":username, :display_name, :role, :password_hash, "
                ":must_change, :request_id, CAST(:source_ip AS inet))"
            ),
            {
                "username": username,
                "display_name": display_name,
                "role": role,
                "password_hash": password_hash,
                "must_change": must_change_password,
                "request_id": request_id,
                "source_ip": source_ip,
            },
        ).scalar_one()
        return UUID(str(value))

    def disable_user(
        self,
        user_id: UUID,
        reason: str,
        request_id: str,
        source_ip: str | None,
    ) -> bool:
        return bool(
            self.session.execute(
                text(
                    "SELECT iam.admin_disable_user("
                    ":user_id, :reason, :request_id, CAST(:source_ip AS inet))"
                ),
                {
                    "user_id": str(user_id),
                    "reason": reason,
                    "request_id": request_id,
                    "source_ip": source_ip,
                },
            ).scalar_one()
        )

    def current_campaign_access(self) -> list[dict[str, Any]]:
        return self._all_dicts(
            self.session.execute(text("SELECT * FROM iam.current_campaign_access()"))
        )

    def available_campaigns(self) -> list[dict[str, Any]]:
        return self._all_dicts(
            self.session.execute(
                text("SELECT * FROM iam.list_requestable_active_campaigns()")
            )
        )

    def submit_access_request(
        self,
        campaign_id: UUID,
        requested_level: str,
        reason: str,
        request_id: str,
        source_ip: str | None,
    ) -> UUID:
        value = self.session.execute(
            text(
                "SELECT iam.submit_campaign_access_request("
                ":campaign_id, :requested_level, :reason, :request_id, "
                "CAST(:source_ip AS inet))"
            ),
            {
                "campaign_id": str(campaign_id),
                "requested_level": requested_level,
                "reason": reason,
                "request_id": request_id,
                "source_ip": source_ip,
            },
        ).scalar_one()
        return UUID(str(value))

    def my_access_requests(self) -> list[dict[str, Any]]:
        return self._all_dicts(
            self.session.execute(text("SELECT * FROM iam.list_my_access_requests()"))
        )

    def cancel_access_request(
        self,
        access_request_id: UUID,
        request_id: str,
        source_ip: str | None,
    ) -> bool:
        return bool(
            self.session.execute(
                text(
                    "SELECT iam.cancel_campaign_access_request("
                    ":access_request_id, :request_id, CAST(:source_ip AS inet))"
                ),
                {
                    "access_request_id": str(access_request_id),
                    "request_id": request_id,
                    "source_ip": source_ip,
                },
            ).scalar_one()
        )

    def admin_access_requests(self, status: str | None) -> list[dict[str, Any]]:
        return self._all_dicts(
            self.session.execute(
                text("SELECT * FROM iam.admin_list_access_requests(:status)"),
                {"status": status},
            )
        )

    def decide_access_request(
        self,
        access_request_id: UUID,
        decision: str,
        reason: str,
        request_id: str,
        source_ip: str | None,
    ) -> bool:
        return bool(
            self.session.execute(
                text(
                    "SELECT iam.admin_decide_campaign_access_request("
                    ":access_request_id, :decision, :reason, :request_id, "
                    "CAST(:source_ip AS inet))"
                ),
                {
                    "access_request_id": str(access_request_id),
                    "decision": decision,
                    "reason": reason,
                    "request_id": request_id,
                    "source_ip": source_ip,
                },
            ).scalar_one()
        )

    def list_user_grants(self, user_id: UUID) -> list[dict[str, Any]]:
        return self._all_dicts(
            self.session.execute(
                text(
                    "SELECT * FROM iam.admin_list_user_campaign_grants(:user_id)"
                ),
                {"user_id": str(user_id)},
            )
        )

    def set_user_grant(
        self,
        user_id: UUID,
        campaign_id: UUID,
        access_level: str,
        reason: str,
        request_id: str,
        source_ip: str | None,
    ) -> bool:
        return bool(
            self.session.execute(
                text(
                    "SELECT iam.admin_set_user_campaign_grant("
                    ":user_id, :campaign_id, :access_level, :reason, :request_id, "
                    "CAST(:source_ip AS inet))"
                ),
                {
                    "user_id": str(user_id),
                    "campaign_id": str(campaign_id),
                    "access_level": access_level,
                    "reason": reason,
                    "request_id": request_id,
                    "source_ip": source_ip,
                },
            ).scalar_one()
        )

    def revoke_user_grant(
        self,
        user_id: UUID,
        campaign_id: UUID,
        reason: str,
        request_id: str,
        source_ip: str | None,
    ) -> bool:
        return bool(
            self.session.execute(
                text(
                    "SELECT iam.admin_revoke_user_campaign_grant("
                    ":user_id, :campaign_id, :reason, :request_id, "
                    "CAST(:source_ip AS inet))"
                ),
                {
                    "user_id": str(user_id),
                    "campaign_id": str(campaign_id),
                    "reason": reason,
                    "request_id": request_id,
                    "source_ip": source_ip,
                },
            ).scalar_one()
        )

    def list_security_events(self, limit: int, offset: int) -> list[dict[str, Any]]:
        return self._all_dicts(
            self.session.execute(
                text(
                    """
                    SELECT
                        e.public_id,
                        e.event_type,
                        u.public_id AS actor_public_id,
                        u.username AS actor_username,
                        e.username,
                        e.outcome,
                        e.request_id,
                        e.source_ip::text AS source_ip,
                        e.metadata,
                        e.created_at
                    FROM audit.security_event e
                    LEFT JOIN iam.app_user u ON u.id = e.actor_user_id
                    ORDER BY e.created_at DESC, e.id DESC
                    LIMIT :limit OFFSET :offset
                    """
                ),
                {"limit": limit, "offset": offset},
            )
        )
