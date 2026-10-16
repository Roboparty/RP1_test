"""Enforce versioned, target-specific asset codes.

Revision ID: 20260918_0013
Revises: 20260918_0012
Create Date: 2026-09-18
"""

from __future__ import annotations

from pathlib import Path

from alembic import op


revision = "20260918_0013"
down_revision = "20260918_0012"
branch_labels = None
depends_on = None

SQL_ROOT = Path(__file__).resolve().parents[2] / "sql"


def upgrade() -> None:
    sql = (SQL_ROOT / "013_asset_code_invariant.sql").read_text(encoding="utf-8")
    op.get_bind().exec_driver_sql(sql.replace("%", "%%"))


def downgrade() -> None:
    raise RuntimeError(
        "Restore the rolling backup instead of weakening the asset code invariant."
    )
