"""Expose explicit API access states without weakening RLS.

Revision ID: 20260917_0010
Revises: 20260917_0009
Create Date: 2026-09-17
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from alembic import op


revision = "20260917_0010"
down_revision = "20260917_0009"
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
    sql = (SQL_ROOT / "010_api_access_status.sql").read_text(encoding="utf-8")
    sql = sql.replace("__RP1_APP_USER__", _role("RP1_APP_USER", "rp1_app"))
    sql = sql.replace(
        "__RP1_READONLY_USER__",
        _role("RP1_READONLY_USER", "rp1_readonly"),
    )
    op.get_bind().exec_driver_sql(sql.replace("%", "%%"))


def downgrade() -> None:
    raise RuntimeError("Restore the rolling backup instead of downgrading access semantics.")
