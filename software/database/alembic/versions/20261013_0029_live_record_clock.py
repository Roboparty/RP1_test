"""Unify the exposure clock and accept only a fresh HMI heartbeat as RUNNING."""
import os
import re
from pathlib import Path

from alembic import op

revision = "20261013_0029"
down_revision = "20261012_0028"
branch_labels = None
depends_on = None


def upgrade() -> None:
    role = os.environ.get("RP1_APP_USER", "rp1_app")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$]*", role):
        raise RuntimeError("Invalid RP1_APP_USER")
    source = Path(__file__).resolve().parents[2] / "sql/029_live_record_clock.sql"
    statement = source.read_text().replace("__RP1_APP_USER__", role)
    op.get_bind().exec_driver_sql(statement.replace("%", "%%"))


def downgrade() -> None:
    raise RuntimeError("Live-running correction rewrites existing execution status and codes")
