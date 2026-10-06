"""Create PostgreSQL extensions and UUIDv7 helper.

Revision ID: 20260917_0001
Revises:
Create Date: 2026-09-17
"""

from __future__ import annotations

from pathlib import Path

from alembic import op


revision = "20260917_0001"
down_revision = None
branch_labels = None
depends_on = None


SQL_ROOT = Path(__file__).resolve().parents[2] / "sql"


def upgrade() -> None:
    op.get_bind().exec_driver_sql(
        (SQL_ROOT / "001_foundation.sql").read_text(encoding="utf-8")
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS public.uuid_v7()")
    op.execute("DROP EXTENSION IF EXISTS pg_trgm")
    op.execute("DROP EXTENSION IF EXISTS btree_gist")
    op.execute("DROP EXTENSION IF EXISTS pgcrypto")
