"""Add edge collector credentials and ingestion lineage.

Revision ID: 20260921_0020
Revises: 20260918_0019
Create Date: 2026-09-21"""
from __future__ import annotations

from alembic import op
from imports.sql_migrations import read_sql

revision = "20260921_0020"
down_revision = "20260918_0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.get_bind().exec_driver_sql(read_sql("020_edge_collector_ingestion.sql").replace("%", "%%"))


def downgrade() -> None:
    raise RuntimeError(
        "Restore the rolling backup instead of deleting ingestion lineage."
    )
