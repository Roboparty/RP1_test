"""Allow campaign readers to see unreviewed joint summaries."""
from pathlib import Path

from alembic import op

revision = "20261015_0036"
down_revision = "20261015_0035"
branch_labels = None
depends_on = None


def upgrade():
    source = Path(__file__).resolve().parents[2] / "sql/036_joint_summary_read.sql"
    op.get_bind().exec_driver_sql(source.read_text().replace("%", "%%"))


def downgrade():
    op.execute("DROP POLICY IF EXISTS historical_recovery_campaign_read ON integration.legacy_joint_summary")
    op.execute("DROP POLICY IF EXISTS historical_recovery_campaign_read ON integration.legacy_recovered_execution")
