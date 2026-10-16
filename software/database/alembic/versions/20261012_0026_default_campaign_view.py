"""Grant VIEW on every campaign to human viewers and executors by default."""
from pathlib import Path

from alembic import op

revision = "20261012_0026"
down_revision = "20261012_0025"
branch_labels = None
depends_on = None


def upgrade():
    source = Path(__file__).resolve().parents[2] / "sql/026_default_campaign_view.sql"
    op.get_bind().exec_driver_sql(source.read_text().replace("%", "%%"))


def downgrade():
    op.execute("DROP TRIGGER IF EXISTS trg_default_campaign_view ON test.test_campaign")
    op.execute("DROP TRIGGER IF EXISTS trg_default_campaign_view ON iam.app_user")
    op.execute("DROP FUNCTION IF EXISTS iam.grant_default_view_for_campaign()")
    op.execute("DROP FUNCTION IF EXISTS iam.grant_default_view_for_user()")
    op.execute("DROP FUNCTION IF EXISTS iam.default_grantor_id()")
