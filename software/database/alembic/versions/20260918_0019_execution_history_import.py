"""Create execution results and CSV import lineage tables.

Revision ID: 20260918_0019
Revises: 20260918_0018
Create Date: 2026-09-18"""
from __future__ import annotations

from alembic import op
from imports.sql_migrations import read_sql

revision = "20260918_0019"
down_revision = "20260918_0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.get_bind().exec_driver_sql(read_sql("019_execution_history_import.sql").replace("%", "%%"))


def downgrade() -> None:
    raise RuntimeError(
        "Restore the rolling backup instead of deleting normalized execution history."
    )
