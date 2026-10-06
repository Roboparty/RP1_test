"""Harden grants on credentials and SECURITY DEFINER entry points.

Revision ID: 20260917_0005
Revises: 20260917_0004
Create Date: 2026-09-17"""
from __future__ import annotations

from alembic import op
from imports.sql_migrations import read_sql, role_identifier

revision = "20260917_0005"
down_revision = "20260917_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.get_bind().exec_driver_sql(read_sql("005_security_hardening.sql").replace("%", "%%"))


def downgrade() -> None:
    app_role = role_identifier("RP1_APP_USER", "rp1_app")
    readonly_role = role_identifier("RP1_READONLY_USER", "rp1_readonly")
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
