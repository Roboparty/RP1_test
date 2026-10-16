"""Index staged performance trend observations."""

from pathlib import Path

from alembic import op


revision = "20260918_0015"
down_revision = "20260918_0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    sql_path = Path(__file__).parents[2] / "sql" / "015_performance_trend_index.sql"
    op.execute(sql_path.read_text(encoding="utf-8"))


def downgrade() -> None:
    op.execute(
        "DROP INDEX IF EXISTS "
        "health.idx_observation_asset_execution_metric_stage_time"
    )
