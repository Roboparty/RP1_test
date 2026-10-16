"""Add browser accounts and Campaign access approval workflow.

Revision ID: 20260918_0016
Revises: 20260918_0015
Create Date: 2026-09-18
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from urllib.parse import urlparse

from alembic import op


revision = "20260918_0016"
down_revision = "20260918_0015"
branch_labels = None
depends_on = None

SQL_ROOT = Path(__file__).resolve().parents[2] / "sql"
ROLE_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")


def _role(name: str, default: str) -> str:
    value = os.environ.get(name, default)
    if not ROLE_PATTERN.fullmatch(value):
        raise RuntimeError(f"{name} is not a valid PostgreSQL role identifier")
    return value


def _migrator_role() -> str:
    configured = os.environ.get("RP1_MIGRATOR_USER")
    if configured:
        if not ROLE_PATTERN.fullmatch(configured):
            raise RuntimeError(
                "RP1_MIGRATOR_USER is not a valid PostgreSQL role identifier"
            )
        return configured
    database_url = os.environ.get("DATABASE_URL", "")
    username = urlparse(database_url).username or "rp1_migrator"
    if not ROLE_PATTERN.fullmatch(username):
        raise RuntimeError("DATABASE_URL username is not a valid PostgreSQL role identifier")
    return username


def upgrade() -> None:
    sql = (SQL_ROOT / "016_account_access_workflow.sql").read_text(encoding="utf-8")
    sql = sql.replace("__RP1_APP_USER__", _role("RP1_APP_USER", "rp1_app"))
    sql = sql.replace(
        "__RP1_READONLY_USER__",
        _role("RP1_READONLY_USER", "rp1_readonly"),
    )
    sql = sql.replace("__RP1_MIGRATOR_USER__", _migrator_role())
    op.get_bind().exec_driver_sql(sql.replace("%", "%%"))


def downgrade() -> None:
    raise RuntimeError(
        "Restore the rolling backup instead of removing account security history."
    )
