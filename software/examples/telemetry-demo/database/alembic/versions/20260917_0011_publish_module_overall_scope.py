"""Publish the independent module OVERALL MTBF scope.

Revision ID: 20260917_0011
Revises: 20260917_0010
Create Date: 2026-09-17
"""

from __future__ import annotations

from pathlib import Path

from alembic import op


revision = "20260917_0011"
down_revision = "20260917_0010"
branch_labels = None
depends_on = None

SQL_ROOT = Path(__file__).resolve().parents[2] / "sql"


def upgrade() -> None:
    sql = (SQL_ROOT / "011_publish_module_overall_scope.sql").read_text(encoding="utf-8")
    op.get_bind().exec_driver_sql(sql.replace("%", "%%"))


def downgrade() -> None:
    raise RuntimeError("Restore the rolling backup instead of retiring a published scope.")
