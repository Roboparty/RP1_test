"""Normalize test target parts and import the 2026-09-18 catalog.

Revision ID: 20260918_0012
Revises: 20260917_0011
Create Date: 2026-09-18
"""

from __future__ import annotations

from pathlib import Path

from alembic import op

from imports.test_case_csv import import_test_cases


revision = "20260918_0012"
down_revision = "20260917_0011"
branch_labels = None
depends_on = None

DATABASE_ROOT = Path(__file__).resolve().parents[2]
SQL_ROOT = DATABASE_ROOT / "sql"
CSV_PATH = DATABASE_ROOT / "imports" / "test_cases_202609181013.csv"


def upgrade() -> None:
    sql = (SQL_ROOT / "012_test_target_parts.sql").read_text(encoding="utf-8")
    connection = op.get_bind()
    connection.exec_driver_sql(sql.replace("%", "%%"))
    import_test_cases(connection, CSV_PATH)
    connection.exec_driver_sql(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM catalog.test_case
                WHERE enabled AND target_part_code IS NULL
            ) THEN
                RAISE EXCEPTION
                    'enabled test cases without a normalized target part remain after CSV import';
            END IF;
        END
        $$;
        ALTER TABLE catalog.test_case
            VALIDATE CONSTRAINT ck_test_case_enabled_target_part;
        """
    )


def downgrade() -> None:
    raise RuntimeError(
        "Restore the rolling backup instead of removing normalized test target assignments."
    )
