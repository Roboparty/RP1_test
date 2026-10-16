"""Add browser accounts and Campaign access approval workflow.

Revision ID: 20260918_0016
Revises: 20260918_0015
Create Date: 2026-09-18"""
from __future__ import annotations

from alembic import op
from imports.sql_migrations import read_sql

revision = "20260918_0016"
down_revision = "20260918_0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.get_bind().exec_driver_sql(read_sql("016_account_access_workflow.sql").replace("%", "%%"))


def downgrade() -> None:
    raise RuntimeError(
        "Restore the rolling backup instead of removing account security history."
    )
