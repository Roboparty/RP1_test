"""Independent summary submission and atomic approval publication."""
import os
import re
from pathlib import Path

from alembic import op

revision = "20261012_0025"
down_revision = "20261008_0024"
branch_labels = None
depends_on = None


def upgrade():
    role = os.environ.get("RP1_APP_USER", "rp1_app")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$]*", role):
        raise RuntimeError("Invalid RP1_APP_USER")
    source = Path(__file__).resolve().parents[2] / "sql/025_test_data_approval.sql"
    op.get_bind().exec_driver_sql(source.read_text().replace("__RP1_APP_USER__", role).replace("%", "%%"))


def downgrade():
    raise RuntimeError("Approval history is immutable; restore a verified backup instead of discarding decisions.")
