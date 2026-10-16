"""Let executors and administrators see and review every summary, including their own."""
from pathlib import Path

from alembic import op

revision = "20261014_0031"
down_revision = "20261014_0030"
branch_labels = None
depends_on = None


def upgrade():
    source = Path(__file__).resolve().parents[2] / "sql/031_submission_visibility.sql"
    op.get_bind().exec_driver_sql(source.read_text().replace("%", "%%"))


def downgrade():
    raise RuntimeError("Submission visibility stays expanded; restore a verified backup to reinstate the old policy.")
