from __future__ import annotations

from functools import lru_cache
from typing import Annotated, Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "RP1 Reliability Platform API"
    app_env: str = "development"
    app_version: str = "0.1.0"
    database_url: str = "postgresql+psycopg://rp1_app:change-this-app-password@postgres:5432/rp1_reliability"
    auth_mode: Literal["session", "development"] = "session"
    dev_user_public_id: str = "00000000-0000-7000-8000-000000000002"
    worker_user_public_id: str = "00000000-0000-7000-8000-000000000001"
    auth_cookie_name: str = "rp1_session"
    auth_csrf_cookie_name: str = "rp1_csrf"
    auth_browser_session_hours: int = Field(default=8, ge=1, le=24)
    auth_cookie_secure: bool | None = None
    service_api_key_hmac_secret: SecretStr = SecretStr(
        "development-only-change-service-api-key-hmac-secret"
    )
    seed_admin_username: str | None = None
    seed_admin_password: SecretStr | None = None
    seed_admin_display_name: str = "本地系统管理员"
    cors_origins: Annotated[list[str], NoDecode] = [
        "http://localhost:5173",
        "http://localhost:8088",
    ]
    worker_poll_seconds: float = Field(default=1.0, gt=0)
    influx_enabled: bool = False
    influx_url: str = "http://influxdb:8086"
    influx_org: str = "rp1_test"
    influx_bucket: str = "telemetry"
    influx_token: SecretStr | None = None
    influx_timeout_seconds: float = Field(default=20.0, gt=0, le=120)

    @property
    def secure_auth_cookie(self) -> bool:
        if self.auth_cookie_secure is not None:
            return self.auth_cookie_secure
        return self.app_env.lower() not in {"development", "dev", "test", "testing"}

    @field_validator("cors_origins", mode="before")
    @classmethod
    def split_origins(cls, value: object) -> object:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @model_validator(mode="after")
    def validate_admin_seed(self) -> Settings:
        has_username = bool(self.seed_admin_username)
        has_password = self.seed_admin_password is not None
        if has_username != has_password:
            raise ValueError(
                "SEED_ADMIN_USERNAME and SEED_ADMIN_PASSWORD must be configured together"
            )
        if has_username:
            username = self.seed_admin_username or ""
            password = self.seed_admin_password.get_secret_value()
            if username != username.strip().lower():
                raise ValueError("SEED_ADMIN_USERNAME must be normalized")
            if len(password) < 12:
                raise ValueError("SEED_ADMIN_PASSWORD must contain at least 12 characters")
            if self.app_env.lower() not in {"development", "dev", "test", "testing"} and (
                username == "local-admin"
                or password == "RP1-Local-Admin-2026!"
            ):
                raise ValueError(
                    "production must override the local example administrator credentials"
                )
        if (
            self.app_env.lower() not in {"development", "dev", "test", "testing"}
            and self.service_api_key_hmac_secret.get_secret_value()
            == "development-only-change-service-api-key-hmac-secret"
        ):
            raise ValueError(
                "production must override SERVICE_API_KEY_HMAC_SECRET"
            )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
