"""Import the audited whole-machine reliability test plan V1.0.

Revision ID: 20260921_0022
Revises: 20260921_0021
Create Date: 2026-09-21
"""

from __future__ import annotations

from pathlib import Path

from alembic import op
from imports.whole_machine_catalog import import_catalog

revision = "20260921_0022"
down_revision = "20260921_0021"
branch_labels = None
depends_on = None

DATABASE_ROOT = Path(__file__).resolve().parents[2]
CATALOG_PATH = DATABASE_ROOT / "imports" / "whole_machine_test_cases_v1_0.json"


def upgrade() -> None:
    import_catalog(op.get_bind(), CATALOG_PATH)


def downgrade() -> None:
    raise RuntimeError(
        "Restore the rolling backup instead of deleting the audited V1.0 test plan."
    )
