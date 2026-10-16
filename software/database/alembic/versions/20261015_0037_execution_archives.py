"""Immutable central CSV archives and versioned human verification."""
from pathlib import Path
import os
import re

from alembic import op

revision = "20261015_0037"
down_revision = "20261015_0036"
branch_labels = None
depends_on = None


def upgrade():
    role = os.environ.get("RP1_APP_USER", "rp1_app")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$]*", role):
        raise RuntimeError("Invalid RP1_APP_USER")
    source = Path(__file__).resolve().parents[2] / "sql/037_execution_archives.sql"
    op.get_bind().exec_driver_sql(source.read_text().replace("__RP1_APP_USER__", role).replace("%", "%%"))


def downgrade():
    raise RuntimeError("Central evidence and audit versions cannot be destructively downgraded; restore a consistent database/file backup.")
