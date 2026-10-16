"""Harden grants on credentials and SECURITY DEFINER entry points.

Revision ID: 20260917_0005
Revises: 20260917_0004
Create Date: 2026-09-17
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from alembic import op


revision = "20260917_0005"
down_revision = "20260917_0004"
branch_labels = None
depends_on = None


SQL_ROOT = Path(__file__).resolve().parents[2] / "sql"
ROLE_PATTERN = re.compile(r"^[a-z_][a-z0-9_]*$")


def _role(name: str, default: str) -> str:
    value = os.environ.get(name, default)
    if not ROLE_PATTERN.fullmatch(value):
        raise RuntimeError(f"unsafe PostgreSQL role name in {name}")
    return value


def upgrade() -> None:
    sql = (SQL_ROOT / "005_security_hardening.sql").read_text(encoding="utf-8")
    sql = sql.replace("rp1_app", _role("RP1_APP_USER", "rp1_app"))
    sql = sql.replace("rp1_readonly", _role("RP1_READONLY_USER", "rp1_readonly"))
    op.get_bind().exec_driver_sql(sql)


def downgrade() -> None:
    app_role = _role("RP1_APP_USER", "rp1_app")
    readonly_role = _role("RP1_READONLY_USER", "rp1_readonly")
    op.execute(
        f"GRANT SELECT ON iam.user_credential, iam.auth_session TO {readonly_role}"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION reliability.claim_recompute_job(text) TO PUBLIC"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION iam.set_request_context(uuid, text, text) TO PUBLIC"
    )
    op.execute(
        f"REVOKE ALL ON iam.user_credential, iam.auth_session FROM {app_role}"
    )
