"""Allow a one-character approval or resubmission reason."""
from pathlib import Path

from alembic import op

revision = "20261014_0032"
down_revision = "20261014_0031"
branch_labels = None
depends_on = None


def upgrade():
    root = Path(__file__).resolve().parents[2] / "sql"
    constraints = (root / "032_submission_reason_length.sql").read_text()
    previous = (root / "031_submission_visibility.sql").read_text()
    functions = previous[previous.index("CREATE OR REPLACE FUNCTION integration.submit_test_data"):]
    functions = functions.replace(
        "length(btrim(p_reason)) NOT BETWEEN 3 AND 300",
        "length(btrim(p_reason)) NOT BETWEEN 1 AND 300",
    )
    op.get_bind().exec_driver_sql((constraints + "\n" + functions).replace("%", "%%"))


def downgrade():
    raise RuntimeError("Short approval reasons stay valid; restore a verified backup to require three characters.")
