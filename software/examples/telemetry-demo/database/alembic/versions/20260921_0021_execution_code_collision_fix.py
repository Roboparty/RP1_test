"""Prevent same-minute execution identifier collisions.

Revision ID: 20260921_0021
Revises: 20260921_0020
Create Date: 2026-09-21
"""

from __future__ import annotations

from pathlib import Path

from alembic import op


revision = "20260921_0021"
down_revision = "20260921_0020"
branch_labels = None
depends_on = None

SQL_ROOT = Path(__file__).resolve().parents[2] / "sql"


def upgrade() -> None:
    sql = (SQL_ROOT / "021_execution_code_collision_fix.sql").read_text(
        encoding="utf-8"
    )
    op.get_bind().exec_driver_sql(sql.replace("%", "%%"))


def downgrade() -> None:
    raise RuntimeError(
        "Restore the rolling backup instead of reintroducing execution-code collisions."
    )
