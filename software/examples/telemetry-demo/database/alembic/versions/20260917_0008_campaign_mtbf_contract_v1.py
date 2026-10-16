"""Implement the current Campaign-driven MTBF contract.

Revision ID: 20260917_0008
Revises: 20260917_0007
Create Date: 2026-09-17"""
from __future__ import annotations

from alembic import op
from imports.sql_migrations import read_sql

revision = "20260917_0008"
down_revision = "20260917_0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.get_bind().exec_driver_sql(read_sql("008_campaign_mtbf_contract_v1.sql").replace("%", "%%"))


def downgrade() -> None:
    raise RuntimeError("Restore the rolling backup instead of downgrading the MTBF contract.")
