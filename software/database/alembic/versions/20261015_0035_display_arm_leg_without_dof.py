"""Show arms and legs without a degree-of-freedom label."""
from pathlib import Path

from alembic import op

revision = "20261015_0035"
down_revision = "20261014_0034"
branch_labels = None
depends_on = None


def upgrade():
    source = Path(__file__).resolve().parents[2] / "sql/035_display_arm_leg_without_dof.sql"
    op.get_bind().exec_driver_sql(source.read_text().replace("%", "%%"))


def downgrade():
    raise RuntimeError("Arm and leg display names stay unified; restore a verified backup to split the labels.")
