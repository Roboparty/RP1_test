"""Fix MTBF V1 projection keys and recompute coalescing.

Revision ID: 20260917_0007
Revises: 20260917_0006
Create Date: 2026-09-17
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from alembic import op


revision = "20260917_0007"
down_revision = "20260917_0006"
branch_labels = None
depends_on = None

SQL_ROOT = Path(__file__).resolve().parents[2] / "sql"
ROLE_PATTERN = re.compile(r"^[a-z_][a-z0-9_]*$")


def _role(name: str, default: str) -> str:
    value = os.environ.get(name, default)
    if not ROLE_PATTERN.fullmatch(value):
        raise RuntimeError(f"unsafe PostgreSQL role name in {name}")
    return value


def upgrade() -> None:
    sql = (SQL_ROOT / "007_mtbf_contract_v1_projection_fix.sql").read_text(
        encoding="utf-8"
    )
    sql = sql.replace("__RP1_APP_USER__", _role("RP1_APP_USER", "rp1_app"))
    op.get_bind().exec_driver_sql(sql.replace("%", "%%"))


def downgrade() -> None:
    raise RuntimeError("Restore the pre-migration backup instead of downgrading V1.")
