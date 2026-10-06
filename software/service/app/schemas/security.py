from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator


Role = Literal["VIEWER", "TEST_EXECUTOR", "SYSTEM_ADMIN"]
PrincipalKind = Literal["HUMAN", "SERVICE"]
AccessLevel = Literal["VIEW", "EDIT"]
AccessRequestStatus = Literal["PENDING", "APPROVED", "REJECTED", "CANCELLED"]


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=1024)


class UserView(BaseModel):
    id: UUID
    username: str
    display_name: str
    role: Role
    kind: PrincipalKind
    enabled: bool
    must_change_password: bool
    created_at: datetime | None = None
    disabled_at: datetime | None = None


class Principal(BaseModel):
    id: UUID
    username: str
    display_name: str
    role: Role
    kind: PrincipalKind
    session_id: UUID | None = None
    session_type: Literal["BROWSER", "DEVELOPMENT", "SERVICE_API"]
    expires_at: datetime | None = None
    must_change_password: bool = False
    csrf_token_hash: str = Field(default="", exclude=True, repr=False)
    credential_id: UUID | None = None
    source_id: UUID | None = None
    source_code: str | None = None

    def user_view(self) -> UserView:
        return UserView(
            id=self.id,
            username=self.username,
            display_name=self.display_name,
            role=self.role,
            kind=self.kind,
            enabled=True,
            must_change_password=self.must_change_password,
        )


class CampaignAccess(BaseModel):
    campaign_id: UUID
    campaign_code: str
    campaign_name: str
    asset_kind: Literal["WHOLE_MACHINE", "MODULE"]
    access_level: AccessLevel
    granted_at: datetime | None = None
    granted_by: UUID | None = None


class AvailableCampaign(BaseModel):
    campaign_id: UUID
    campaign_code: str
    campaign_name: str
    asset_kind: Literal["WHOLE_MACHINE", "MODULE"]
    current_grant: AccessLevel | None = None
    pending_request_id: UUID | None = None
    pending_requested_level: AccessLevel | None = None


class AccessRequest(BaseModel):
    id: UUID
    requester_id: UUID | None = None
    requester_username: str | None = None
    campaign_id: UUID
    campaign_code: str
    campaign_name: str
    requested_level: AccessLevel
    reason: str
    status: AccessRequestStatus
    decision_reason: str | None = None
    created_at: datetime
    updated_at: datetime
    decided_at: datetime | None = None
    decided_by: UUID | None = None


class AuditEvent(BaseModel):
    id: UUID
    event_type: str
    actor_id: UUID | None = None
    actor_username: str | None = None
    username: str
    outcome: Literal["SUCCESS", "FAILURE", "DENIED"]
    request_id: str
    source_ip: str | None = None
    metadata: dict[str, Any]
    created_at: datetime


class LoginResponse(BaseModel):
    user: UserView
    expires_at: datetime


class MeResponse(BaseModel):
    user: UserView
    campaign_access: list[CampaignAccess]
    session_type: Literal["BROWSER", "DEVELOPMENT", "SERVICE_API"]
    expires_at: datetime | None = None


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=1024)
    new_password: str = Field(min_length=12, max_length=1024)

    @model_validator(mode="after")
    def passwords_must_differ(self) -> ChangePasswordRequest:
        if self.current_password == self.new_password:
            raise ValueError("new password must differ from current password")
        return self


class UserCreateRequest(BaseModel):
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=12, max_length=1024)
    display_name: str = Field(min_length=1, max_length=255)
    role: Role
    must_change_password: bool = True

    @field_validator("username")
    @classmethod
    def normalize_username(cls, value: str) -> str:
        normalized = value.strip().lower()
        if not normalized:
            raise ValueError("username is required")
        return normalized

    @field_validator("display_name")
    @classmethod
    def trim_display_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("display_name is required")
        return normalized


class ReasonRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=2048)

    @field_validator("reason")
    @classmethod
    def trim_reason(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("reason is required")
        return normalized


class AccessRequestCreate(ReasonRequest):
    campaign_id: UUID
    requested_level: AccessLevel


class AccessDecision(ReasonRequest):
    pass


class CampaignGrantRequest(ReasonRequest):
    campaign_id: UUID
    access_level: AccessLevel


class ServiceCredentialCreateRequest(BaseModel):
    username: str = Field(min_length=1, max_length=128)
    display_name: str = Field(min_length=1, max_length=255)
    source_code: str = Field(
        min_length=1, max_length=128, pattern=r"^[A-Z0-9]+(?:-[A-Z0-9]+)*$"
    )
    campaign_ids: list[UUID] = Field(min_length=1, max_length=200)
    expires_at: datetime | None = None

    @field_validator("username")
    @classmethod
    def normalize_service_username(cls, value: str) -> str:
        normalized = value.strip().lower()
        if not normalized:
            raise ValueError("username is required")
        return normalized

    @field_validator("display_name")
    @classmethod
    def trim_service_display_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("display_name is required")
        return normalized

    @field_validator("campaign_ids")
    @classmethod
    def campaign_ids_are_unique(cls, value: list[UUID]) -> list[UUID]:
        if len(set(value)) != len(value):
            raise ValueError("campaign_ids must be unique")
        return value


class ServiceCredentialRotateRequest(ReasonRequest):
    expires_at: datetime | None = None
    revoke_previous: bool = True


class ServiceCredentialRevokeRequest(ReasonRequest):
    disable_principal: bool = False


class ServiceCredentialGrant(BaseModel):
    service_id: UUID
    credential_id: UUID
    source_id: UUID | None = None
    api_key: str = Field(repr=False)
    key_id: str
    expires_at: datetime | None = None


class ServiceCredentialRevocation(BaseModel):
    service_id: UUID
    revoked_count: int
    principal_disabled: bool
