"""Implement the RP1 MTBF backend rule contract V1.

Revision ID: 20260917_0006
Revises: 20260917_0005
Create Date: 2026-09-17"""
from __future__ import annotations

from alembic import op
from imports.sql_migrations import read_sql

revision = "20260917_0006"
down_revision = "20260917_0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.get_bind().exec_driver_sql(read_sql("006_mtbf_contract_v1.sql").replace("%", "%%"))


def downgrade() -> None:
    raise RuntimeError(
        "MTBF contract V1 is append-only and cannot be downgraded automatically; "
        "restore the pre-migration backup instead."
    )
