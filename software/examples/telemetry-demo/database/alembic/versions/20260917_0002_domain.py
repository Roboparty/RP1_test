"""Create RP1 reliability domain schemas and tables.

Revision ID: 20260917_0002
Revises: 20260917_0001
Create Date: 2026-09-17
"""

from __future__ import annotations

from pathlib import Path

from alembic import op


revision = "20260917_0002"
down_revision = "20260917_0001"
branch_labels = None
depends_on = None


SQL_ROOT = Path(__file__).resolve().parents[2] / "sql"


def upgrade() -> None:
    op.get_bind().exec_driver_sql(
        (SQL_ROOT / "002_domain.sql").read_text(encoding="utf-8")
    )


def downgrade() -> None:
    for schema in ("audit", "integration", "health", "reliability", "test", "catalog", "iam"):
        op.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
    op.execute("DROP FUNCTION IF EXISTS public.set_updated_at()")
