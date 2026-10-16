"""Attach an approved summary to the live execution when their time ranges overlap."""
from pathlib import Path

from alembic import op

revision = "20261014_0033"
down_revision = "20261014_0032"
branch_labels = None
depends_on = None


def upgrade():
    source = Path(__file__).resolve().parents[2] / "sql/033_approve_existing_live_run.sql"
    op.get_bind().exec_driver_sql(source.read_text().replace("%", "%%"))


def downgrade():
    raise RuntimeError("Approved summaries stay linked to the existing live run; restore a verified backup to split them.")
