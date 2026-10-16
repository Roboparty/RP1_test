"""Allow conflict-safe enqueue visibility only within the collector's source."""
from __future__ import annotations
from alembic import op
from imports.sql_migrations import read_sql

revision = '20261006_0026'
down_revision = '20261006_0025'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.get_bind().exec_driver_sql(read_sql('026_influx_source_select.sql'))


def downgrade() -> None:
    op.execute('DROP POLICY influx_source_select ON integration.influx_export_queue')
