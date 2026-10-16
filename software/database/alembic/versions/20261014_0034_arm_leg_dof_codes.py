"""Separate full-degree ARM and LEG codes from the reduced-degree S codes."""
from pathlib import Path

from alembic import op

revision = "20261014_0034"
down_revision = "20261014_0033"
branch_labels = None
depends_on = None


def upgrade():
    source = Path(__file__).resolve().parents[2] / "sql/034_arm_leg_dof_codes.sql"
    op.get_bind().exec_driver_sql(source.read_text().replace("%", "%%"))


def downgrade():
    raise RuntimeError("Full-degree historical records stay on ARM and LEG; restore a verified backup to put the S back.")
