"""Normalize and import the audited legacy execution history.

Revision ID: 20260918_0019
Revises: 20260918_0018
Create Date: 2026-09-18
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from alembic import op

from imports.execution_csv import import_execution_history


revision = "20260918_0019"
down_revision = "20260918_0018"
branch_labels = None
depends_on = None

DATABASE_ROOT = Path(__file__).resolve().parents[2]
SQL_ROOT = DATABASE_ROOT / "sql"
CSV_PATH = DATABASE_ROOT / "imports" / "executions_202609181314.csv"
ROLE_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")


def _role(name: str, default: str) -> str:
    value = os.environ.get(name, default)
    if not ROLE_PATTERN.fullmatch(value):
        raise RuntimeError(f"{name} is not a valid PostgreSQL role identifier")
    return value


def upgrade() -> None:
    sql = (SQL_ROOT / "019_execution_history_import.sql").read_text(
        encoding="utf-8"
    )
    sql = sql.replace("__RP1_APP_USER__", _role("RP1_APP_USER", "rp1_app"))
    sql = sql.replace(
        "__RP1_READONLY_USER__",
        _role("RP1_READONLY_USER", "rp1_readonly"),
    )
    connection = op.get_bind()
    connection.exec_driver_sql(sql.replace("%", "%%"))
    # Fresh commercial installations contain no customer execution history.
    # An existing deployment may opt in using its separately supplied source CSV.
    if os.environ.get("IMPORT_LEGACY_HISTORY", "false").lower() == "true":
        source = Path(os.environ.get("LEGACY_EXECUTION_CSV", str(CSV_PATH)))
        import_execution_history(connection, source)


def downgrade() -> None:
    raise RuntimeError(
        "Restore the rolling backup instead of deleting normalized execution history."
    )
