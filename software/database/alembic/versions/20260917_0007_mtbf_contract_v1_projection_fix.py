"""Fix MTBF V1 projection keys and recompute coalescing.

Revision ID: 20260917_0007
Revises: 20260917_0006
Create Date: 2026-09-17"""
from __future__ import annotations

from alembic import op
from imports.sql_migrations import read_sql

revision = "20260917_0007"
down_revision = "20260917_0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.get_bind().exec_driver_sql(read_sql("007_mtbf_contract_v1_projection_fix.sql").replace("%", "%%"))


def downgrade() -> None:
    raise RuntimeError("Restore the pre-migration backup instead of downgrading V1.")
