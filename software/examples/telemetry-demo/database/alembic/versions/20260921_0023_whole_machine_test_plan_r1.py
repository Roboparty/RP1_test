"""Apply the approved whole-machine test plan R1 decisions.

Revision ID: 20260921_0023
Revises: 20260921_0022
Create Date: 2026-09-21
"""

from __future__ import annotations

from pathlib import Path

from alembic import op
from imports.whole_machine_revision import import_revision

revision = "20260921_0023"
down_revision = "20260921_0022"
branch_labels = None
depends_on = None

DATABASE_ROOT = Path(__file__).resolve().parents[2]
SOURCE_PATH = DATABASE_ROOT / "imports" / "whole_machine_test_cases_v1_0.json"
REVISION_PATH = DATABASE_ROOT / "imports" / "whole_machine_test_plan_r1.json"


def upgrade() -> None:
    import_revision(op.get_bind(), SOURCE_PATH, REVISION_PATH)


def downgrade() -> None:
    raise RuntimeError(
        "Restore the rolling backup instead of discarding the approved R1 decision."
    )
