"""Add edge collector credentials and ingestion lineage.

Revision ID: 20260921_0020
Revises: 20260918_0019
Create Date: 2026-09-21
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from alembic import op


revision = "20260921_0020"
down_revision = "20260918_0019"
branch_labels = None
depends_on = None

SQL_ROOT = Path(__file__).resolve().parents[2] / "sql"
ROLE_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")


def _role(name: str, default: str) -> str:
    value = os.environ.get(name, default)
    if not ROLE_PATTERN.fullmatch(value):
        raise RuntimeError(f"{name} is not a valid PostgreSQL role identifier")
    return value


def upgrade() -> None:
    sql = (SQL_ROOT / "020_edge_collector_ingestion.sql").read_text(
        encoding="utf-8"
    )
    sql = sql.replace("__RP1_APP_USER__", _role("RP1_APP_USER", "rp1_app"))
    sql = sql.replace(
        "__RP1_READONLY_USER__",
        _role("RP1_READONLY_USER", "rp1_readonly"),
    )
    op.get_bind().exec_driver_sql(sql.replace("%", "%%"))


def downgrade() -> None:
    raise RuntimeError(
        "Restore the rolling backup instead of deleting ingestion lineage."
    )
