"""Add explicit whole-machine right-arm demo subjects without weakening module boundaries."""
from __future__ import annotations
from alembic import op
from imports.sql_migrations import read_sql

revision = '20261006_0025'
down_revision = '20261006_0024'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.get_bind().exec_driver_sql(read_sql('025_whole_machine_demo_subjects.sql').replace('%', '%%'))


def downgrade() -> None:
    raise RuntimeError('Keep telemetry subject history; restore a reviewed snapshot instead.')
