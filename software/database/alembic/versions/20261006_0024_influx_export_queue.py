"""Add durable Influx export and explicit synthetic/real source classification."""
from __future__ import annotations

from alembic import op
from imports.sql_migrations import read_sql

revision = '20261006_0024'
down_revision = '20260921_0023'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.get_bind().exec_driver_sql(read_sql("024_influx_export_queue.sql").replace("%", "%%"))


def downgrade() -> None:
    raise RuntimeError('Exported evidence must be retained; restore a reviewed database snapshot.')
