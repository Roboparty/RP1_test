"""Expose explicit API access states without weakening RLS.

Revision ID: 20260917_0010
Revises: 20260917_0009
Create Date: 2026-09-17"""
from __future__ import annotations

from alembic import op
from imports.sql_migrations import read_sql

revision = "20260917_0010"
down_revision = "20260917_0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.get_bind().exec_driver_sql(read_sql("010_api_access_status.sql").replace("%", "%%"))


def downgrade() -> None:
    raise RuntimeError("Restore the rolling backup instead of downgrading access semantics.")
