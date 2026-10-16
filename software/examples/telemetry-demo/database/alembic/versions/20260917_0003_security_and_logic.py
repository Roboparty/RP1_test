"""Add invariants, RLS, automatic audit and recompute queue logic.

Revision ID: 20260917_0003
Revises: 20260917_0002
Create Date: 2026-09-17"""
from __future__ import annotations

from alembic import op
from imports.sql_migrations import read_sql

revision = "20260917_0003"
down_revision = "20260917_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.get_bind().exec_driver_sql(read_sql("003_security_and_logic.sql").replace("%", "%%"))


def downgrade() -> None:
    # The domain downgrade drops all affected schemas, policies and triggers.
    pass
