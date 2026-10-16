"""Implement the RP1 MTBF backend rule contract V1.

Revision ID: 20260917_0006
Revises: 20260917_0005
Create Date: 2026-09-17
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from alembic import op


revision = "20260917_0006"
down_revision = "20260917_0005"
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
    sql = (SQL_ROOT / "006_mtbf_contract_v1.sql").read_text(encoding="utf-8")
    sql = sql.replace("__RP1_APP_USER__", _role("RP1_APP_USER", "rp1_app"))
    sql = sql.replace(
        "__RP1_READONLY_USER__", _role("RP1_READONLY_USER", "rp1_readonly")
    )
    # Psycopg treats PL/pgSQL percent signs as client-side placeholders.
    op.get_bind().exec_driver_sql(sql.replace("%", "%%"))


def downgrade() -> None:
    raise RuntimeError(
        "MTBF contract V1 is append-only and cannot be downgraded automatically; "
        "restore the pre-migration backup instead."
    )
