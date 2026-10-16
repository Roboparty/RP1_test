"""Track authenticated HMI connectivity separately from test results."""

import os
import re
from pathlib import Path

from alembic import op


revision = "20261008_0024"
down_revision = "20260921_0023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    role = os.environ.get("RP1_APP_USER", "rp1_app")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$]*", role):
        raise RuntimeError("Invalid RP1_APP_USER")
    source = Path(__file__).resolve().parents[2] / "sql/024_hmi_presence.sql"
    statement = source.read_text().replace("__RP1_APP_USER__", role)
    op.get_bind().exec_driver_sql(statement.replace("%", "%%"))


def downgrade() -> None:
    op.execute("DROP FUNCTION integration.list_hmi_presence(uuid, integer)")
    op.execute("DROP FUNCTION integration.disconnect_hmi_presence(uuid, uuid)")
    op.execute("DROP FUNCTION integration.record_hmi_presence(uuid, uuid, uuid, text, text, bigint, double precision, jsonb, jsonb)")
    op.execute("DROP TABLE integration.hmi_presence")
