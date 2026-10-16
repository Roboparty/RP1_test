"""Seed only system catalogs, without fake business data.

Revision ID: 20260917_0004
Revises: 20260917_0003
Create Date: 2026-09-17
"""

from __future__ import annotations

from pathlib import Path

from alembic import op


revision = "20260917_0004"
down_revision = "20260917_0003"
branch_labels = None
depends_on = None


SQL_ROOT = Path(__file__).resolve().parents[2] / "sql"


def upgrade() -> None:
    op.get_bind().exec_driver_sql(
        (SQL_ROOT / "004_system_seed.sql").read_text(encoding="utf-8")
    )


def downgrade() -> None:
    op.execute("DELETE FROM integration.retention_policy WHERE version = '1.0-draft'")
    op.execute("DELETE FROM reliability.statistics_method WHERE method_code = 'UNCONFIGURED'")
    op.execute("DELETE FROM reliability.mtbf_scope WHERE version = '1.0-draft'")
    op.execute(
        "DELETE FROM catalog.mission_tag WHERE tag_code IN ("
        "'GENERAL_OPERATION','LOAD_WALK','LOAD_STAND','GRASP','FACTORY_WORK',"
        "'DEMO_INTERACTION','HIGH_DIFFICULTY','FAULT_INJECTION','ENVIRONMENTAL_EVIDENCE')"
    )
    op.execute("DELETE FROM integration.ingestion_source WHERE source_code IN ('SYSTEM','MANUAL')")
