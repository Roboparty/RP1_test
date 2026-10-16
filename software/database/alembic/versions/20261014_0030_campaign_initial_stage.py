"""Give every test plan an initial stage named 测试阶段1."""
import os
import re
from pathlib import Path

from alembic import op

revision = "20261014_0030"
down_revision = "20261013_0029"
branch_labels = None
depends_on = None


def upgrade():
    source = Path(__file__).resolve().parents[2] / "sql/030_campaign_initial_stage.sql"
    statement = source.read_text()
    for name, default in (("RP1_APP_USER", "rp1_app"), ("RP1_READONLY_USER", "rp1_readonly")):
        role = os.environ.get(name, default)
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$]*", role):
            raise RuntimeError(f"Invalid {name}")
        statement = statement.replace(f"__{name}__", role)
    op.get_bind().exec_driver_sql(statement.replace("%", "%%"))


def downgrade():
    raise RuntimeError("Initial campaign stages and workstation case contexts are kept")
