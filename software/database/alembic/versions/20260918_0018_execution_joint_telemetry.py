"""Add normalized execution joint telemetry series.

Revision ID: 20260918_0018
Revises: 20260918_0017
Create Date: 2026-09-18"""
from __future__ import annotations

from alembic import op
from imports.sql_migrations import read_sql

revision = "20260918_0018"
down_revision = "20260918_0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.get_bind().exec_driver_sql(read_sql("018_execution_joint_telemetry.sql").replace("%", "%%"))


def downgrade() -> None:
    raise RuntimeError(
        "Restore the rolling backup instead of removing normalized telemetry history."
    )
