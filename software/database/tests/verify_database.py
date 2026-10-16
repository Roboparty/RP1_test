from __future__ import annotations

import json
import os
import sys
from contextlib import contextmanager

import psycopg
from psycopg import sql
from psycopg.errors import (
    CheckViolation,
    ExclusionViolation,
    InsufficientPrivilege,
    RaiseException,
    UniqueViolation,
)


def dsn() -> str:
    url = os.environ["DATABASE_URL"]
    return url.replace("postgresql+psycopg://", "postgresql://", 1)


@contextmanager
def savepoint(connection: psycopg.Connection, name: str):
    with connection.cursor() as cursor:
        cursor.execute(sql.SQL("SAVEPOINT {}").format(sql.Identifier(name)))
    try:
        yield
    except Exception:
        with connection.cursor() as cursor:
            cursor.execute(sql.SQL("ROLLBACK TO SAVEPOINT {}").format(sql.Identifier(name)))
        raise
    else:
        with connection.cursor() as cursor:
            cursor.execute(sql.SQL("RELEASE SAVEPOINT {}").format(sql.Identifier(name)))


def scalar(cursor: psycopg.Cursor, statement: str, params=()):
    if params:
        cursor.execute(statement, params)
    else:
        cursor.execute(statement)
    row = cursor.fetchone()
    return None if row is None else row[0]


def assert_raises(connection: psycopg.Connection, expected, statement: str, params=()):
    try:
        with savepoint(connection, "expected_failure"):
            with connection.cursor() as cursor:
                cursor.execute(statement, params)
    except expected:
        return
    raise AssertionError(f"Expected {expected}, but statement succeeded")


def main() -> int:
    results: list[dict[str, object]] = []

    def check(name: str, condition: bool, evidence: object = None) -> None:
        if not condition:
            raise AssertionError(f"{name}: {evidence!r}")
        results.append({"name": name, "pass": True, "evidence": evidence})

    with psycopg.connect(dsn(), autocommit=False) as connection:
        with connection.cursor() as cursor:
            schemas = set(
                row[0]
                for row in cursor.execute(
                    "SELECT schema_name FROM information_schema.schemata "
                    "WHERE schema_name = ANY(%s)",
                    (["iam", "catalog", "test", "reliability", "health", "integration", "audit"],),
                )
            )
            check("seven domain schemas exist", len(schemas) == 7, sorted(schemas))

            extensions = set(
                row[0]
                for row in cursor.execute(
                    "SELECT extname FROM pg_extension WHERE extname = ANY(%s)",
                    (["pgcrypto", "btree_gist", "pg_trgm"],),
                )
            )
            check("required extensions exist", len(extensions) == 3, sorted(extensions))

            csv_count = scalar(
                cursor,
                "SELECT count(*) FROM catalog.test_case "
                "WHERE EXISTS (SELECT 1 FROM catalog.test_case_version v "
                "WHERE v.test_case_id = catalog.test_case.id "
                "AND v.version = 'CSV-20260918' AND v.status = 'PUBLISHED')",
            )
            check("CSV test case catalog has exactly 60 rows", csv_count == 60, csv_count)
            cursor.execute(
                """
                SELECT status, count(*)
                FROM catalog.test_case_version
                WHERE version = 'DOCX-V1.0-20260921'
                GROUP BY status
                ORDER BY status
                """
            )
            whole_machine_version_counts = dict(cursor.fetchall())
            check(
                "whole-machine V1.0 preserves source publication blockers",
                whole_machine_version_counts == {"DRAFT": 2, "PUBLISHED": 17},
                whole_machine_version_counts,
            )
            whole_machine_source_hash_count = scalar(
                cursor,
                """
                SELECT count(*)
                FROM catalog.test_case_version
                WHERE version = 'DOCX-V1.0-20260921'
                  AND source_references @> %s::jsonb
                """,
                (
                    (
                        '[{"sha256": '
                        '"66e211d8cad1296539ca8c00c4b0b342da6a87affb51ad97ec13c225b99502d1"}]'
                    ),
                ),
            )
            check(
                "whole-machine V1.0 versions retain source SHA-256",
                whole_machine_source_hash_count == 19,
                whole_machine_source_hash_count,
            )
            approved_walking_revision = scalar(
                cursor,
                """
                SELECT count(*)
                FROM catalog.test_case_version version
                JOIN catalog.test_case test_case
                  ON test_case.id = version.test_case_id
                WHERE test_case.case_code = 'SYS-REL-002'
                  AND version.version = 'DOCX-V1.0-R1-20260921'
                  AND version.status = 'PUBLISHED'
                  AND version.procedure_spec #>>
                      '{matrix,primary_stress_or_duration}' =
                      '单电池连续段≤1.5 h；累计50 km及300次姿态循环'
                  AND EXISTS (
                      SELECT 1
                      FROM jsonb_array_elements_text(
                          version.procedure_spec -> 'steps'
                      ) step
                      WHERE step =
                          '2. 每5 km执行30次原地转向；累计姿态循环不少于300次。'
                  )
                """,
            )
            check(
                "approved walking revision uses 50 km and 300 cycles",
                approved_walking_revision == 1,
                approved_walking_revision,
            )
            removed_rain_case = scalar(
                cursor,
                """
                SELECT count(*)
                FROM catalog.test_case
                WHERE case_code = 'SYS-REL-015' AND NOT enabled
                """,
            )
            check(
                "SYS-REL-015 is removed from the active catalog",
                removed_rain_case == 1,
                removed_rain_case,
            )
            cursor.execute(
                "SELECT target_part_code, count(*) FROM catalog.test_case "
                "WHERE enabled GROUP BY target_part_code ORDER BY target_part_code"
            )
            part_counts = dict(cursor.fetchall())
            check(
                "enabled test case part counts are normalized",
                part_counts
                == {
                    "BAT": 1,
                    "CHEST": 9,
                    "HEAD": 1,
                    "LOWER": 10,
                    "SARM": 11,
                    "SLEG": 10,
                    "SYS": 18,
                    "UPPER": 10,
                },
                part_counts,
            )
            enabled_null = scalar(
                cursor,
                "SELECT count(*) FROM catalog.test_case "
                "WHERE enabled AND target_part_code IS NULL",
            )
            check("enabled test cases always have a target part", enabled_null == 0, enabled_null)
            mismatch_count = scalar(
                cursor,
                "SELECT count(*) FROM test.test_execution e "
                "JOIN test.asset a ON a.id=e.asset_id "
                "JOIN catalog.test_case_version v ON v.id=e.test_case_version_id "
                "JOIN catalog.test_case tc ON tc.id=v.test_case_id "
                "LEFT JOIN test.module_profile mp ON mp.asset_id=a.id "
                "WHERE (a.asset_kind='WHOLE_MACHINE' AND tc.target_part_code IS DISTINCT FROM 'SYS') "
                "OR (a.asset_kind='MODULE' AND tc.target_part_code IS DISTINCT FROM mp.target_part_code)",
            )
            check("existing executions match asset target parts", mismatch_count == 0, mismatch_count)
            invalid_asset_count = scalar(
                cursor,
                "SELECT count(*) FROM test.asset "
                "WHERE asset_code !~ '^RP[0-9]+\\.[0-9]+-[A-Z][A-Z0-9]*-[0-9]{3}$'",
            )
            check("invalid-format asset count is zero", invalid_asset_count == 0, invalid_asset_count)
            segment_mismatch_count = scalar(
                cursor,
                "SELECT count(*) FROM test.asset a "
                "LEFT JOIN test.module_profile mp ON mp.asset_id=a.id "
                "LEFT JOIN catalog.test_target_part part ON part.part_code=mp.target_part_code "
                "WHERE (a.asset_kind='WHOLE_MACHINE' AND split_part(a.asset_code,'-',2)<>'SYS') "
                "OR (a.asset_kind='MODULE' AND mp.asset_id IS NOT NULL "
                "AND split_part(a.asset_code,'-',2) IS DISTINCT FROM part.asset_code_segment)",
            )
            check(
                "asset/profile segment mismatch count is zero",
                segment_mismatch_count == 0,
                segment_mismatch_count,
            )
            legacy_asset_count = scalar(
                cursor,
                "SELECT count(*) FROM test.asset WHERE asset_code LIKE 'SHOWCASE-%%'",
            )
            check(
                "legacy SHOWCASE asset code count is zero",
                legacy_asset_count == 0,
                legacy_asset_count,
            )
            invalid_execution_code_count = scalar(
                cursor,
                r"""
                SELECT count(*)
                FROM test.test_execution
                WHERE execution_code !~
                    '^[A-Z0-9]+(-[A-Z0-9]+)*_[0-9]{10}_RP[0-9]+\.[0-9]+-(SARM|SLEG|SYS|UPPER|LOWER|CHEST|HEAD|BAT)-[0-9]{3}_[A-Z0-9]+(-[A-Z0-9]+)*(_E[0-9]+)?$'
                """,
            )
            check(
                "execution identifiers use case-time-asset-station format",
                invalid_execution_code_count == 0,
                invalid_execution_code_count,
            )
            execution_component_mismatch_count = scalar(
                cursor,
                """
                SELECT count(*)
                FROM test.test_execution e
                JOIN catalog.test_case_version tcv ON tcv.id=e.test_case_version_id
                JOIN catalog.test_case tc ON tc.id=tcv.test_case_id
                JOIN test.asset a ON a.id=e.asset_id
                JOIN test.station s ON s.id=e.station_id
                WHERE e.execution_code IS DISTINCT FROM test.compose_execution_code(
                          tc.case_code, e.code_timestamp, a.asset_code, s.station_code
                      )
                  AND e.execution_code IS DISTINCT FROM test.compose_execution_code_v2(
                          tc.case_code, e.code_timestamp, a.asset_code, s.station_code, e.id
                      )
                """,
            )
            check(
                "execution identifier components match linked records",
                execution_component_mismatch_count == 0,
                execution_component_mismatch_count,
            )

            cursor.execute(
                """
                SELECT
                    (SELECT count(*)
                     FROM integration.execution_import_record record
                     JOIN integration.ingestion_source source
                       ON source.id=record.source_id
                     WHERE source.source_code='LEGACY-EXECUTION-CSV-20260918'),
                    (SELECT count(*)
                     FROM test.execution_result result
                     JOIN integration.execution_import_record record
                       ON record.execution_id=result.execution_id
                     JOIN integration.ingestion_source source
                       ON source.id=record.source_id
                     WHERE source.source_code='LEGACY-EXECUTION-CSV-20260918'),
                    (SELECT count(*)
                     FROM test.test_execution execution
                     JOIN integration.ingestion_source source
                       ON source.id=execution.source_id
                     WHERE source.source_code='LEGACY-EXECUTION-CSV-20260918')
                """
            )
            execution_import_counts = cursor.fetchone()
            check(
                "execution CSV has exactly 135 lineage result and execution rows",
                execution_import_counts == (135, 135, 135),
                execution_import_counts,
            )
            invalid_lineage_count = scalar(
                cursor,
                """
                SELECT count(*)
                FROM integration.execution_import_record record
                JOIN integration.ingestion_source source
                  ON source.id=record.source_id
                WHERE source.source_code='LEGACY-EXECUTION-CSV-20260918'
                  AND (
                      record.raw_payload->>'test_id'
                          IS DISTINCT FROM record.source_execution_key
                      OR jsonb_typeof(record.raw_payload) <> 'object'
                      OR jsonb_typeof(record.normalization_warnings) <> 'array'
                      OR record.source_file <> 'executions_202609181314.csv'
                      OR record.source_row_number NOT BETWEEN 2 AND 136
                      OR record.row_hash !~ '^[0-9a-f]{64}$'
                  )
                """,
            )
            check(
                "execution lineage preserves valid raw rows hashes and warnings",
                invalid_lineage_count == 0,
                invalid_lineage_count,
            )
            cursor.execute(
                """
                SELECT test_case.case_code, test_case.target_part_code, count(*)
                FROM integration.execution_import_record record
                JOIN integration.ingestion_source source
                  ON source.id=record.source_id
                JOIN test.test_execution execution
                  ON execution.id=record.execution_id
                JOIN catalog.test_case_version version
                  ON version.id=execution.test_case_version_id
                JOIN catalog.test_case test_case
                  ON test_case.id=version.test_case_id
                WHERE source.source_code='LEGACY-EXECUTION-CSV-20260918'
                GROUP BY test_case.case_code, test_case.target_part_code
                ORDER BY test_case.case_code
                """
            )
            imported_case_mapping = {
                case_code: (part_code, count)
                for case_code, part_code, count in cursor.fetchall()
            }
            check(
                "execution import maps all nine cases to case-defined parts",
                imported_case_mapping
                == {
                    "PERF-SARM-001": ("SARM", 5),
                    "REL-LOWER-001": ("LOWER", 32),
                    "REL-LOWER-002": ("LOWER", 11),
                    "REL-SARM-001": ("SARM", 6),
                    "REL-SARM-002": ("SARM", 18),
                    "REL-SLEG-001": ("SLEG", 5),
                    "REL-SLEG-002": ("SLEG", 9),
                    "REL-SLEG-010": ("SLEG", 6),
                    "REL-UPPER-001": ("UPPER", 43),
                },
                imported_case_mapping,
            )
            cursor.execute(
                """
                SELECT asset_kind, target_part_code, domain,
                       count(version.id) FILTER (
                           WHERE version.status='PUBLISHED'
                       )
                FROM catalog.test_case test_case
                LEFT JOIN catalog.test_case_version version
                  ON version.test_case_id=test_case.id
                WHERE test_case.case_code='PERF-SARM-001'
                GROUP BY asset_kind, target_part_code, domain
                """
            )
            performance_case = cursor.fetchone()
            check(
                "missing performance case has a published import version",
                performance_case == ("MODULE", "SARM", "PERFORMANCE", 1),
                performance_case,
            )
            imported_assets = set(
                row[0]
                for row in cursor.execute(
                    """
                    SELECT DISTINCT asset.asset_code
                    FROM integration.execution_import_record record
                    JOIN integration.ingestion_source source
                      ON source.id=record.source_id
                    JOIN test.test_execution execution
                      ON execution.id=record.execution_id
                    JOIN test.asset asset ON asset.id=execution.asset_id
                    WHERE source.source_code='LEGACY-EXECUTION-CSV-20260918'
                    """
                )
            )
            check(
                "execution import created seven normalized legacy assets",
                imported_assets
                == {
                    "RP1.3-LOWER-001",
                    "RP1.3-SARM-001",
                    "RP1.3-SARM-002",
                    "RP1.3-SARM-003",
                    "RP1.3-SLEG-001",
                    "RP1.3-SLEG-002",
                    "RP1.3-UPPER-001",
                },
                sorted(imported_assets),
            )
            imported_part_mismatch = scalar(
                cursor,
                """
                SELECT count(*)
                FROM integration.execution_import_record record
                JOIN integration.ingestion_source source
                  ON source.id=record.source_id
                JOIN test.test_execution execution
                  ON execution.id=record.execution_id
                JOIN test.asset asset ON asset.id=execution.asset_id
                JOIN test.module_profile profile ON profile.asset_id=asset.id
                JOIN catalog.test_case_version version
                  ON version.id=execution.test_case_version_id
                JOIN catalog.test_case test_case
                  ON test_case.id=version.test_case_id
                WHERE source.source_code='LEGACY-EXECUTION-CSV-20260918'
                  AND (
                      asset.product_family <> 'RP1.3-LEGACY'
                      OR split_part(asset.asset_code, '-', 2)
                          IS DISTINCT FROM profile.target_part_code
                      OR profile.target_part_code
                          IS DISTINCT FROM test_case.target_part_code
                  )
                """,
            )
            check(
                "legacy execution assets profiles and cases have matching parts",
                imported_part_mismatch == 0,
                imported_part_mismatch,
            )
            cursor.execute(
                """
                SELECT
                    count(DISTINCT execution.campaign_id),
                    count(DISTINCT execution.asset_id),
                    count(DISTINCT execution.cycle_id),
                    count(DISTINCT execution.configuration_snapshot_id),
                    count(DISTINCT execution.station_id)
                FROM test.test_execution execution
                JOIN integration.ingestion_source source
                  ON source.id=execution.source_id
                WHERE source.source_code='LEGACY-EXECUTION-CSV-20260918'
                """
            )
            imported_context_counts = cursor.fetchone()
            imported_segment_count = scalar(
                cursor,
                """
                SELECT count(*)
                FROM test.analysis_segment segment
                JOIN test.test_cycle cycle ON cycle.id=segment.cycle_id
                JOIN test.test_campaign campaign ON campaign.id=cycle.campaign_id
                WHERE campaign.campaign_code LIKE 'LEGACY-EXEC-%-20260918'
                """,
            )
            check(
                "legacy import has four campaigns seven contexts and three stations",
                imported_context_counts == (4, 7, 7, 7, 3)
                and imported_segment_count == 7,
                {
                    "contexts": imported_context_counts,
                    "segments": imported_segment_count,
                },
            )
            cursor.execute(
                """
                SELECT result.source_status, count(*)
                FROM test.execution_result result
                JOIN integration.execution_import_record record
                  ON record.execution_id=result.execution_id
                JOIN integration.ingestion_source source
                  ON source.id=record.source_id
                WHERE source.source_code='LEGACY-EXECUTION-CSV-20260918'
                GROUP BY result.source_status
                """
            )
            imported_source_statuses = dict(cursor.fetchall())
            check(
                "source execution statuses are retained exactly",
                imported_source_statuses
                == {
                    "passed": 89,
                    "failed": 17,
                    "blocked": 18,
                    "scheduled": 9,
                    "running": 2,
                },
                imported_source_statuses,
            )
            cursor.execute(
                """
                SELECT execution.status, count(*)
                FROM test.test_execution execution
                JOIN integration.ingestion_source source
                  ON source.id=execution.source_id
                WHERE source.source_code='LEGACY-EXECUTION-CSV-20260918'
                GROUP BY execution.status
                """
            )
            imported_execution_statuses = dict(cursor.fetchall())
            check(
                "semantic execution status normalization is deterministic",
                imported_execution_statuses
                == {
                    "COMPLETED": 79,
                    "BLOCKED": 28,
                    "FAILED": 17,
                    "SCHEDULED": 9,
                    "RUNNING": 2,
                },
                imported_execution_statuses,
            )
            cursor.execute(
                """
                SELECT result.outcome, count(*)
                FROM test.execution_result result
                JOIN integration.execution_import_record record
                  ON record.execution_id=result.execution_id
                JOIN integration.ingestion_source source
                  ON source.id=record.source_id
                WHERE source.source_code='LEGACY-EXECUTION-CSV-20260918'
                GROUP BY result.outcome
                """
            )
            imported_outcomes = dict(cursor.fetchall())
            check(
                "semantic execution outcomes are deterministic",
                imported_outcomes
                == {
                    "PASSED": 40,
                    "FAILED": 17,
                    "INCONCLUSIVE": 67,
                    "NOT_EVALUATED": 11,
                },
                imported_outcomes,
            )
            cursor.execute(
                """
                SELECT result.termination_kind, count(*)
                FROM test.execution_result result
                JOIN integration.execution_import_record record
                  ON record.execution_id=result.execution_id
                JOIN integration.ingestion_source source
                  ON source.id=record.source_id
                WHERE source.source_code='LEGACY-EXECUTION-CSV-20260918'
                GROUP BY result.termination_kind
                """
            )
            imported_terminations = dict(cursor.fetchall())
            check(
                "semantic termination kinds are deterministic",
                imported_terminations
                == {
                    "NORMAL": 40,
                    "MANUAL_STOP": 51,
                    "SAFETY_WATCHDOG": 19,
                    "DATA_TIMEOUT": 1,
                    "SYSTEM_CRASH": 5,
                    "SCHEDULED": 9,
                    "RUNNING": 2,
                    "UNKNOWN": 8,
                },
                imported_terminations,
            )
            cursor.execute(
                """
                SELECT warning->>'code', count(*)
                FROM integration.execution_import_record record
                JOIN integration.ingestion_source source
                  ON source.id=record.source_id
                CROSS JOIN LATERAL jsonb_array_elements(
                    record.normalization_warnings
                ) warning
                WHERE source.source_code='LEGACY-EXECUTION-CSV-20260918'
                GROUP BY warning->>'code'
                """
            )
            imported_warning_counts = dict(cursor.fetchall())
            check(
                "normalization warnings retain audited corrections and conflicts",
                imported_warning_counts.get("asset_code_normalized") == 134
                and imported_warning_counts.get(
                    "case_part_overrode_source_part"
                )
                == 60
                and imported_warning_counts.get(
                    "reported_duration_mismatch"
                )
                == 6
                and imported_warning_counts.get(
                    "source_status_semantic_conflict"
                )
                == 49,
                imported_warning_counts,
            )
            cursor.execute(
                """
                SELECT
                    count(*),
                    count(*) FILTER (
                        WHERE interval.clock_quality='PARTIAL'
                          AND interval.data_quality='PARTIAL'
                    )
                FROM test.runtime_interval interval
                JOIN integration.ingestion_source source
                  ON source.id=interval.source_id
                WHERE source.source_code='LEGACY-EXECUTION-CSV-20260918'
                  AND interval.source_kind='LEGACY_REPORTED'
                """
            )
            imported_runtime_counts = cursor.fetchone()
            non_conservative_runtime_count = scalar(
                cursor,
                """
                SELECT count(*)
                FROM test.runtime_interval interval
                JOIN integration.ingestion_source source
                  ON source.id=interval.source_id
                JOIN integration.execution_import_record record
                  ON record.execution_id=interval.execution_id
                 AND record.source_id=source.id
                JOIN test.execution_result result
                  ON result.execution_id=interval.execution_id
                WHERE source.source_code='LEGACY-EXECUTION-CSV-20260918'
                  AND (
                      interval.active_seconds
                          > result.reported_duration_seconds + 0.001
                      OR interval.active_seconds
                          > extract(epoch FROM (
                              interval.ended_at - interval.started_at
                          )) + 0.001
                      OR interval.active_seconds
                          > extract(epoch FROM (
                              result.last_data_at - interval.started_at
                          )) + 0.001
                  )
                """,
            )
            check(
                "legacy runtimes are conservative and timing differences are partial",
                imported_runtime_counts == (124, 12)
                and non_conservative_runtime_count == 0,
                {
                    "runtime_counts": imported_runtime_counts,
                    "non_conservative": non_conservative_runtime_count,
                },
            )
            imported_event_count = scalar(
                cursor,
                """
                SELECT count(*)
                FROM test.test_event event
                JOIN integration.ingestion_source source
                  ON source.id=event.source_id
                WHERE source.source_code='LEGACY-EXECUTION-CSV-20260918'
                  AND event.source_event_key
                      LIKE '%:issue-or-termination'
                """,
            )
            missing_issue_event_count = scalar(
                cursor,
                """
                SELECT count(*)
                FROM test.execution_result result
                JOIN integration.execution_import_record record
                  ON record.execution_id=result.execution_id
                JOIN integration.ingestion_source source
                  ON source.id=record.source_id
                WHERE source.source_code='LEGACY-EXECUTION-CSV-20260918'
                  AND (
                      btrim(result.issues) <> ''
                      OR result.exception_count > 0
                      OR result.termination_kind NOT IN (
                          'NORMAL', 'SCHEDULED', 'RUNNING'
                      )
                      OR result.outcome IN ('FAILED', 'INCONCLUSIVE')
                  )
                  AND NOT EXISTS (
                      SELECT 1
                      FROM test.test_event event
                      WHERE event.source_id=record.source_id
                        AND event.source_event_key=
                            record.source_execution_key
                            || ':issue-or-termination'
                  )
                """,
            )
            check(
                "issues and abnormal terminations have idempotent events",
                imported_event_count == 85
                and missing_issue_event_count == 0,
                {
                    "events": imported_event_count,
                    "missing": missing_issue_event_count,
                },
            )
            cursor.execute(
                """
                SELECT
                    count(*),
                    count(*) FILTER (
                        WHERE artifact.availability_status='ARCHIVED'
                          AND artifact.object_key IS NULL
                    )
                FROM integration.artifact artifact
                JOIN test.test_execution execution
                  ON execution.id=artifact.execution_id
                JOIN integration.ingestion_source source
                  ON source.id=execution.source_id
                WHERE source.source_code='LEGACY-EXECUTION-CSV-20260918'
                  AND artifact.kind='LEGACY_REPORT_REFERENCE'
                """
            )
            imported_artifact_counts = cursor.fetchone()
            check(
                "legacy report references never claim live availability",
                imported_artifact_counts == (7, 7),
                imported_artifact_counts,
            )
            formal_mtbf_inclusion_count = scalar(
                cursor,
                """
                SELECT
                    (SELECT count(*)
                     FROM reliability.exposure_assessment assessment
                     JOIN test.runtime_interval interval
                       ON interval.id=assessment.runtime_interval_id
                     JOIN integration.ingestion_source source
                       ON source.id=interval.source_id
                     WHERE source.source_code=
                         'LEGACY-EXECUTION-CSV-20260918')
                    +
                    (SELECT count(*)
                     FROM reliability.mtbf_eligible_runtime eligible
                     JOIN test.runtime_interval interval
                       ON interval.id=eligible.runtime_interval_id
                     JOIN integration.ingestion_source source
                       ON source.id=interval.source_id
                     WHERE source.source_code=
                         'LEGACY-EXECUTION-CSV-20260918')
                """,
            )
            check(
                "legacy imported runtime is not formal MTBF exposure",
                formal_mtbf_inclusion_count == 0,
                formal_mtbf_inclusion_count,
            )
            new_table_security = scalar(
                cursor,
                """
                SELECT count(*)
                FROM pg_class relation
                JOIN pg_namespace namespace
                  ON namespace.oid=relation.relnamespace
                WHERE (
                    namespace.nspname,
                    relation.relname,
                    relation.relrowsecurity
                ) IN (
                    ('test', 'execution_result', true),
                    ('integration', 'execution_import_record', true)
                )
                  AND has_table_privilege(
                      %s,
                      namespace.nspname || '.' || relation.relname,
                      'SELECT,INSERT,UPDATE'
                  )
                  AND has_table_privilege(
                      %s,
                      namespace.nspname || '.' || relation.relname,
                      'SELECT'
                  )
                """,
                (
                    os.environ.get("RP1_APP_USER", "rp1_app"),
                    os.environ.get("RP1_READONLY_USER", "rp1_readonly"),
                ),
            )
            check(
                "new execution tables have RLS and least-privilege grants",
                new_table_security == 2,
                new_table_security,
            )

            table_count = scalar(
                cursor,
                "SELECT count(*) FROM information_schema.tables "
                "WHERE table_schema = ANY(%s) AND table_type = 'BASE TABLE'",
                (["iam", "catalog", "test", "reliability", "health", "integration", "audit"],),
            )
            check("core table count", table_count >= 55, table_count)

            rls_count = scalar(
                cursor,
                "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
                "WHERE n.nspname = ANY(%s) AND c.relkind='r' AND c.relrowsecurity",
                (["iam", "catalog", "test", "reliability", "health", "integration", "audit"],),
            )
            check("RLS enabled on protected tables", rls_count >= 45, rls_count)

            ingestion_tables = set(
                row[0]
                for row in cursor.execute(
                    "SELECT table_schema || '.' || table_name "
                    "FROM information_schema.tables "
                    "WHERE (table_schema, table_name) IN ("
                    "('iam','service_credential'),"
                    "('integration','service_principal_source'),"
                    "('integration','ingestion_batch'),"
                    "('integration','trajectory_version'))"
                )
            )
            check(
                "edge ingestion credential and lineage tables exist",
                ingestion_tables
                == {
                    "iam.service_credential",
                    "integration.service_principal_source",
                    "integration.ingestion_batch",
                    "integration.trajectory_version",
                },
                sorted(ingestion_tables),
            )
            credential_hash_constraints = scalar(
                cursor,
                """
                SELECT count(*)
                FROM pg_constraint constraint_definition
                JOIN pg_class relation
                  ON relation.oid=constraint_definition.conrelid
                JOIN pg_namespace namespace
                  ON namespace.oid=relation.relnamespace
                WHERE namespace.nspname='iam'
                  AND relation.relname='service_credential'
                  AND pg_get_constraintdef(constraint_definition.oid)
                      LIKE '%secret_hash%'
                  AND pg_get_constraintdef(constraint_definition.oid)
                      LIKE '%64%'
                """,
            )
            check(
                "service credentials enforce irreversible hash shape",
                credential_hash_constraints >= 1,
                credential_hash_constraints,
            )
            app_role = os.environ.get("RP1_APP_USER", "rp1_app")
            check(
                "shared application role cannot read service hashes",
                not scalar(
                    cursor,
                    "SELECT has_table_privilege(%s, "
                    "'iam.service_credential', 'SELECT')",
                    (app_role,),
                ),
            )
            ingestion_indexes = set(
                row[0]
                for row in cursor.execute(
                    "SELECT indexname FROM pg_indexes "
                    "WHERE (schemaname, tablename) IN ("
                    "('integration','ingestion_receipt'),"
                    "('integration','ingestion_batch'),"
                    "('integration','trajectory_version'),"
                    "('integration','artifact'))"
                )
            )
            check(
                "ingestion producer and file hash indexes exist",
                {
                    "uq_ingestion_receipt_producer_key",
                    "ingestion_batch_source_id_producer_batch_key_key",
                    "ix_trajectory_hash",
                    "uq_artifact_source_key",
                }
                <= ingestion_indexes,
                sorted(ingestion_indexes),
            )

            telemetry_series_columns = set(
                row[0]
                for row in cursor.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema='health' AND table_name='metric_series'"
                )
            )
            required_series_columns = {
                "public_id",
                "campaign_id",
                "asset_id",
                "cycle_id",
                "segment_id",
                "execution_id",
                "stage_id",
                "configuration_snapshot_id",
                "metric_version_id",
                "subject_code",
                "series_kind",
                "cycle_index",
                "started_at",
                "ended_at",
                "point_count",
                "sampling_interval_ms",
                "downsample_method",
                "quality_status",
                "raw_data_reference",
                "source_id",
                "source_series_key",
                "created_at",
                "updated_at",
            }
            check(
                "metric series has normalized header columns",
                required_series_columns <= telemetry_series_columns,
                sorted(required_series_columns - telemetry_series_columns),
            )
            observation_series_columns = set(
                row[0]
                for row in cursor.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema='health' AND table_name='metric_observation' "
                    "AND column_name IN ('series_id','sample_index')"
                )
            )
            check(
                "metric observations expose series sample coordinates",
                observation_series_columns == {"series_id", "sample_index"},
                sorted(observation_series_columns),
            )
            telemetry_indexes = set(
                row[0]
                for row in cursor.execute(
                    "SELECT indexname FROM pg_indexes "
                    "WHERE schemaname='health' "
                    "AND tablename IN ('metric_series','metric_observation')"
                )
            )
            check(
                "telemetry uniqueness and query indexes exist",
                {
                    "uq_metric_series_source_key",
                    "ix_metric_series_execution_subject",
                    "uq_metric_observation_series_sample",
                    "ix_metric_observation_series_time",
                }
                <= telemetry_indexes,
                sorted(telemetry_indexes),
            )

            cursor.execute(
                "SELECT target_part_code, count(*) "
                "FROM catalog.telemetry_subject WHERE enabled "
                "GROUP BY target_part_code ORDER BY target_part_code"
            )
            subject_part_counts = dict(cursor.fetchall())
            check(
                "telemetry subject directory covers every target part",
                subject_part_counts
                == {
                    "BAT": 3,
                    "CHEST": 3,
                    "HEAD": 3,
                    "LOWER": 14,
                    "SARM": 7,
                    "SLEG": 6,
                    "SYS": 3,
                    "UPPER": 12,
                },
                subject_part_counts,
            )
            invalid_subject_codes = scalar(
                cursor,
                "SELECT count(*) FROM catalog.telemetry_subject "
                "WHERE subject_code !~ '^[A-Z0-9]+(-[A-Z0-9]+)*$'",
            )
            check(
                "telemetry subject identifiers are normalized",
                invalid_subject_codes == 0,
                invalid_subject_codes,
            )
            duplicate_subject_directory_entries = scalar(
                cursor,
                """
                SELECT count(*)
                FROM (
                    SELECT subject_code
                    FROM catalog.telemetry_subject
                    GROUP BY subject_code
                    HAVING count(*) > 1
                    UNION ALL
                    SELECT target_part_code || ':' || display_order::text
                    FROM catalog.telemetry_subject
                    GROUP BY target_part_code, display_order
                    HAVING count(*) > 1
                ) duplicates
                """,
            )
            check(
                "telemetry subject codes and display orders are unique",
                duplicate_subject_directory_entries == 0,
                duplicate_subject_directory_entries,
            )

            cursor.execute(
                """
                SELECT definition.metric_code, version.canonical_unit
                FROM catalog.metric_definition definition
                JOIN catalog.metric_version version
                  ON version.metric_definition_id=definition.id
                WHERE version.version='1.0.0'
                  AND version.status='PUBLISHED'
                  AND definition.metric_code LIKE 'JOINT-%'
                ORDER BY definition.metric_code
                """
            )
            published_telemetry_metrics = dict(cursor.fetchall())
            check(
                "eight execution telemetry metrics are published",
                published_telemetry_metrics
                == {
                    "JOINT-ACTUAL-POSITION": "deg",
                    "JOINT-CURRENT": "A",
                    "JOINT-TARGET-POSITION": "deg",
                    "JOINT-TEMPERATURE": "°C",
                    "JOINT-TORQUE": "N·m",
                    "JOINT-TRACKING-ERROR": "deg",
                    "JOINT-VELOCITY": "deg/s",
                    "JOINT-VIBRATION-RMS": "mm/s",
                },
                published_telemetry_metrics,
            )

            showcase_asset_count = scalar(
                cursor,
                "SELECT count(*) FROM test.asset "
                "WHERE product_family='RP1-SHOWCASE' AND NOT voided",
            )
            check(
                "showcase retains all twelve assets",
                showcase_asset_count == 12,
                showcase_asset_count,
            )
            showcase_series_count = scalar(
                cursor,
                "SELECT count(*) FROM health.metric_series "
                "WHERE source_series_key LIKE 'SHOWCASE-TELEMETRY-%'",
            )
            showcase_point_count = scalar(
                cursor,
                "SELECT count(*) FROM health.metric_observation observation "
                "JOIN health.metric_series series ON series.id=observation.series_id "
                "WHERE series.source_series_key LIKE 'SHOWCASE-TELEMETRY-%'",
            )
            check(
                "showcase telemetry has controlled data volume",
                showcase_series_count == 804
                and showcase_point_count == 80_400,
                {
                    "series": showcase_series_count,
                    "points": showcase_point_count,
                },
            )

            inconsistent_series_points = scalar(
                cursor,
                """
                SELECT count(*)
                FROM (
                    SELECT
                        series.id,
                        series.point_count,
                        count(observation.id) AS actual_count,
                        min(observation.sample_index) AS first_sample,
                        max(observation.sample_index) AS last_sample,
                        count(DISTINCT observation.sample_index) AS distinct_samples
                    FROM health.metric_series series
                    LEFT JOIN health.metric_observation observation
                      ON observation.series_id=series.id
                    WHERE series.source_series_key LIKE 'SHOWCASE-TELEMETRY-%'
                    GROUP BY series.id
                ) counted
                WHERE point_count IS DISTINCT FROM actual_count
                   OR actual_count <> 100
                   OR first_sample <> 0
                   OR last_sample <> 99
                   OR distinct_samples <> 100
                """,
            )
            check(
                "every telemetry series has samples zero through ninety-nine",
                inconsistent_series_points == 0,
                inconsistent_series_points,
            )
            context_mismatch_count = scalar(
                cursor,
                """
                SELECT count(*)
                FROM health.metric_observation observation
                JOIN health.metric_series series ON series.id=observation.series_id
                WHERE ROW(
                    observation.campaign_id,
                    observation.asset_id,
                    observation.cycle_id,
                    observation.segment_id,
                    observation.execution_id,
                    observation.stage_id,
                    observation.configuration_snapshot_id,
                    observation.metric_version_id
                ) IS DISTINCT FROM ROW(
                    series.campaign_id,
                    series.asset_id,
                    series.cycle_id,
                    series.segment_id,
                    series.execution_id,
                    series.stage_id,
                    series.configuration_snapshot_id,
                    series.metric_version_id
                )
                   OR observation.observed_at < series.started_at
                   OR observation.observed_at > series.ended_at
                """,
            )
            check(
                "series and point contexts agree",
                context_mismatch_count == 0,
                context_mismatch_count,
            )

            cursor.execute(
                """
                SELECT
                    asset.asset_code,
                    count(DISTINCT series.cycle_index) AS cycles,
                    count(DISTINCT definition.metric_code) AS metrics
                FROM test.asset asset
                JOIN health.metric_series series ON series.asset_id=asset.id
                JOIN catalog.metric_version version
                  ON version.id=series.metric_version_id
                JOIN catalog.metric_definition definition
                  ON definition.id=version.metric_definition_id
                WHERE asset.product_family='RP1-SHOWCASE'
                  AND series.source_series_key LIKE 'SHOWCASE-TELEMETRY-%'
                GROUP BY asset.asset_code
                ORDER BY asset.asset_code
                """
            )
            asset_telemetry_coverage = cursor.fetchall()
            check(
                "all showcase assets have three cycles and eight metrics",
                len(asset_telemetry_coverage) == 12
                and all(
                    cycles == 3 and metrics == 8
                    for _, cycles, metrics in asset_telemetry_coverage
                ),
                asset_telemetry_coverage,
            )
            covered_parts = set(
                row[0]
                for row in cursor.execute(
                    """
                    SELECT DISTINCT subject.target_part_code
                    FROM health.metric_series series
                    JOIN catalog.telemetry_subject subject
                      ON subject.subject_code=series.subject_code
                    WHERE series.source_series_key LIKE 'SHOWCASE-TELEMETRY-%'
                    """
                )
            )
            check(
                "showcase telemetry covers all normalized parts",
                covered_parts
                == {
                    "BAT",
                    "CHEST",
                    "HEAD",
                    "LOWER",
                    "SARM",
                    "SLEG",
                    "SYS",
                    "UPPER",
                },
                sorted(covered_parts),
            )

            cursor.execute(
                """
                SELECT raw_data_reference->>'scenario', count(*)
                FROM health.metric_series
                WHERE source_series_key LIKE 'SHOWCASE-TELEMETRY-%'
                GROUP BY raw_data_reference->>'scenario'
                """
            )
            scenario_counts = dict(cursor.fetchall())
            check(
                "showcase includes normal and five abnormal scenarios",
                set(scenario_counts)
                == {
                    "NORMAL",
                    "TRACKING_DRIFT",
                    "IMPACT_TORQUE",
                    "TEMPERATURE_RISE",
                    "VIBRATION",
                    "QUALITY_GAP",
                }
                and scenario_counts["NORMAL"] > 0
                and sum(
                    count
                    for scenario, count in scenario_counts.items()
                    if scenario != "NORMAL"
                )
                > 0,
                scenario_counts,
            )
            degraded_points = scalar(
                cursor,
                """
                SELECT count(*)
                FROM health.metric_observation observation
                JOIN health.metric_series series ON series.id=observation.series_id
                WHERE series.source_series_key LIKE 'SHOWCASE-TELEMETRY-%'
                  AND observation.quality_status IN ('PARTIAL','INVALID')
                """,
            )
            check(
                "quality gap scenarios contain degraded samples",
                degraded_points > 0,
                degraded_points,
            )
            array_reference_count = scalar(
                cursor,
                """
                SELECT
                    (SELECT count(*) FROM health.metric_series
                     WHERE source_series_key LIKE 'SHOWCASE-TELEMETRY-%'
                       AND (
                           jsonb_typeof(raw_data_reference) <> 'object'
                           OR raw_data_reference ?| ARRAY['points','samples','values']
                       ))
                    +
                    (SELECT count(*)
                     FROM health.metric_observation observation
                     JOIN health.metric_series series
                       ON series.id=observation.series_id
                     WHERE series.source_series_key LIKE 'SHOWCASE-TELEMETRY-%'
                       AND (
                           jsonb_typeof(observation.raw_data_reference) <> 'object'
                           OR observation.raw_data_reference
                              ?| ARRAY['points','samples','values']
                       ))
                """,
            )
            check(
                "telemetry values are rows rather than JSON point arrays",
                array_reference_count == 0,
                array_reference_count,
            )
            source_key_issues = scalar(
                cursor,
                """
                SELECT count(*)
                FROM (
                    SELECT source_id, source_series_key
                    FROM health.metric_series
                    WHERE source_series_key LIKE 'SHOWCASE-TELEMETRY-%'
                    GROUP BY source_id, source_series_key
                    HAVING source_id IS NULL
                        OR source_series_key IS NULL
                        OR count(*) <> 1
                ) invalid
                """,
            )
            check(
                "series source idempotency keys are complete and unique",
                source_key_issues == 0,
                source_key_issues,
            )

            waveform_identity_error = scalar(
                cursor,
                """
                WITH joint_points AS (
                    SELECT
                        series.asset_id,
                        series.subject_code,
                        series.cycle_index,
                        observation.sample_index,
                        max(observation.canonical_numeric_value) FILTER (
                            WHERE definition.metric_code='JOINT-TARGET-POSITION'
                        ) AS target_position,
                        max(observation.canonical_numeric_value) FILTER (
                            WHERE definition.metric_code='JOINT-ACTUAL-POSITION'
                        ) AS actual_position,
                        max(observation.canonical_numeric_value) FILTER (
                            WHERE definition.metric_code='JOINT-TRACKING-ERROR'
                        ) AS tracking_error
                    FROM health.metric_series series
                    JOIN catalog.telemetry_subject subject
                      ON subject.subject_code=series.subject_code
                     AND subject.subject_kind='JOINT'
                    JOIN catalog.metric_version version
                      ON version.id=series.metric_version_id
                    JOIN catalog.metric_definition definition
                      ON definition.id=version.metric_definition_id
                    JOIN health.metric_observation observation
                      ON observation.series_id=series.id
                    WHERE series.source_series_key LIKE 'SHOWCASE-TELEMETRY-%'
                      AND definition.metric_code IN (
                          'JOINT-TARGET-POSITION',
                          'JOINT-ACTUAL-POSITION',
                          'JOINT-TRACKING-ERROR'
                      )
                      AND observation.canonical_numeric_value IS NOT NULL
                    GROUP BY
                        series.asset_id,
                        series.subject_code,
                        series.cycle_index,
                        observation.sample_index
                )
                SELECT max(abs(
                    actual_position - target_position - tracking_error
                ))
                FROM joint_points
                WHERE target_position IS NOT NULL
                  AND actual_position IS NOT NULL
                  AND tracking_error IS NOT NULL
                """,
            )
            check(
                "actual position equals target plus tracking error",
                waveform_identity_error is not None
                and waveform_identity_error <= 0.000001,
                str(waveform_identity_error),
            )

            app_role = os.environ.get("RP1_APP_USER", "rp1_app")
            cursor.execute(
                "SELECT rolsuper, rolcreatedb, rolcreaterole, rolbypassrls "
                "FROM pg_roles WHERE rolname=%s",
                (app_role,),
            )
            role_flags = cursor.fetchone()
            check("application role is non-privileged", role_flags == (False, False, False, False), role_flags)

            for protected_role in (
                os.environ.get("RP1_APP_USER", "rp1_app"),
                os.environ.get("RP1_READONLY_USER", "rp1_readonly"),
            ):
                cursor.execute(sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(protected_role)))
                assert_raises(
                    connection,
                    InsufficientPrivilege,
                    "SELECT count(*) FROM iam.user_credential",
                )
                cursor.execute("RESET ROLE")
            check("shared roles cannot read credential material", True)

            uuid_versions = [
                scalar(cursor, "SELECT substring(public.uuid_v7()::text, 15, 1)")
                for _ in range(8)
            ]
            check("public identifiers are UUIDv7", set(uuid_versions) == {"7"}, uuid_versions)

            # Minimal transactional fixture. It is rolled back and never becomes demo data.
            cursor.execute(
                "INSERT INTO iam.app_user(username,display_name,role,must_change_password) "
                "VALUES ('verify-admin','Verify Admin','SYSTEM_ADMIN',false),"
                "('verify-executor','Verify Executor','TEST_EXECUTOR',false),"
                "('verify-viewer','Verify Viewer','VIEWER',false) "
                "RETURNING id, public_id, role"
            )
            users = {role: (user_id, public_id) for user_id, public_id, role in cursor.fetchall()}
            admin_id, admin_public_id = users["SYSTEM_ADMIN"]
            executor_id, executor_public_id = users["TEST_EXECUTOR"]
            viewer_id, viewer_public_id = users["VIEWER"]

            site_id = scalar(cursor, "INSERT INTO test.site(site_code,name) VALUES ('VERIFY-SITE','Verify Site') RETURNING id")
            lab_id = scalar(cursor, "INSERT INTO test.lab(site_id,lab_code,name) VALUES (%s,'VERIFY-LAB','Verify Lab') RETURNING id", (site_id,))
            station_id = scalar(cursor, "INSERT INTO test.station(lab_id,station_code,name,station_type) VALUES (%s,'VERIFY-STATION','Verify Station','SHARED') RETURNING id", (lab_id,))
            whole_batch = scalar(cursor, "INSERT INTO test.manufacturing_batch(batch_code,asset_kind,product_family) VALUES ('VERIFY-WHOLE-BATCH','WHOLE_MACHINE','RP1') RETURNING id")
            module_batch = scalar(cursor, "INSERT INTO test.manufacturing_batch(batch_code,asset_kind,product_family) VALUES ('VERIFY-MODULE-BATCH','MODULE','RP1') RETURNING id")
            whole_asset = scalar(cursor, "INSERT INTO test.asset(asset_code,asset_kind,product_family,batch_id,serial_number) VALUES ('RP99.1-SYS-901','WHOLE_MACHINE','RP1',%s,'VERIFY-W-001') RETURNING id", (whole_batch,))
            module_asset = scalar(cursor, "INSERT INTO test.asset(asset_code,asset_kind,product_family,batch_id,serial_number) VALUES ('RP99.1-SLEG-902','MODULE','RP1',%s,'VERIFY-M-001') RETURNING id", (module_batch,))
            check("valid structured asset codes are accepted", whole_asset is not None and module_asset is not None)
            assert_raises(
                connection,
                CheckViolation,
                "INSERT INTO test.asset(asset_code,asset_kind,product_family,batch_id,serial_number) VALUES ('VERIFY-BAD','MODULE','RP1',%s,'VERIFY-BAD-001')",
                (module_batch,),
            )
            check("invalid asset code format is rejected", True)
            assert_raises(
                connection,
                CheckViolation,
                "INSERT INTO test.asset(asset_code,asset_kind,product_family,batch_id,serial_number) VALUES ('RP99.1-FOO-904','MODULE','RP1',%s,'VERIFY-UNKNOWN-PART')",
                (module_batch,),
            )
            check("unknown asset part segment is rejected", True)
            assert_raises(
                connection,
                UniqueViolation,
                "INSERT INTO test.asset(asset_code,asset_kind,product_family,batch_id,serial_number) VALUES ('RP99.1-SLEG-902','MODULE','RP1',%s,'VERIFY-M-UNIQUE')",
                (module_batch,),
            )
            check("asset code uniqueness remains enforced", True)
            mismatch_asset = scalar(
                cursor,
                "INSERT INTO test.asset(asset_code,asset_kind,product_family,batch_id,serial_number) VALUES ('RP99.1-SARM-903','MODULE','RP1',%s,'VERIFY-M-MISMATCH') RETURNING id",
                (module_batch,),
            )
            assert_raises(
                connection,
                RaiseException,
                "INSERT INTO test.module_profile(asset_id,module_type,target_part_code) VALUES (%s,'KNEE','SLEG')",
                (mismatch_asset,),
            )
            check("asset segment and module target mismatch is rejected", True)
            cursor.execute("INSERT INTO test.whole_machine_profile(asset_id,model) VALUES (%s,'RP1')", (whole_asset,))
            cursor.execute(
                "INSERT INTO test.module_profile(asset_id,module_type,target_part_code) "
                "VALUES (%s,'KNEE','SLEG')",
                (module_asset,),
            )
            program_id = scalar(cursor, "INSERT INTO test.test_program(program_code,name,status,owner_id) VALUES ('VERIFY-PROGRAM','Verify Program','ACTIVE',%s) RETURNING id", (admin_id,))
            whole_campaign = scalar(cursor, "INSERT INTO test.test_campaign(program_id,site_id,campaign_code,name,asset_kind,status) VALUES (%s,%s,'VERIFY-WHOLE-C','Verify Whole Campaign','WHOLE_MACHINE','ACTIVE') RETURNING id", (program_id, site_id))
            module_campaign = scalar(cursor, "INSERT INTO test.test_campaign(program_id,site_id,campaign_code,name,asset_kind,status) VALUES (%s,%s,'VERIFY-MODULE-C','Verify Module Campaign','MODULE','ACTIVE') RETURNING id", (program_id, site_id))
            cursor.execute("INSERT INTO test.campaign_asset(campaign_id,asset_id) VALUES (%s,%s)", (whole_campaign, whole_asset))
            cursor.execute("INSERT INTO test.campaign_asset(campaign_id,asset_id) VALUES (%s,%s)", (module_campaign, module_asset))
            cursor.execute("INSERT INTO iam.user_campaign_access(user_id,campaign_id,access_level,granted_by) VALUES (%s,%s,'EDIT',%s),(%s,%s,'VIEW',%s)", (executor_id, whole_campaign, admin_id, viewer_id, whole_campaign, admin_id))

            whole_campaign_public_id = scalar(
                cursor,
                "SELECT public_id FROM test.test_campaign WHERE id=%s",
                (whole_campaign,),
            )
            module_campaign_public_id = scalar(
                cursor,
                "SELECT public_id FROM test.test_campaign WHERE id=%s",
                (module_campaign,),
            )
            cursor.execute(
                "SELECT iam.set_request_context(%s, 'verify-access-state', 'database verification')",
                (viewer_public_id,),
            )
            viewer_read_state = scalar(
                cursor,
                "SELECT iam.campaign_access_state(%s, false)",
                (whole_campaign_public_id,),
            )
            viewer_edit_state = scalar(
                cursor,
                "SELECT iam.campaign_access_state(%s, true)",
                (whole_campaign_public_id,),
            )
            unauthorized_state = scalar(
                cursor,
                "SELECT iam.campaign_access_state(%s, false)",
                (module_campaign_public_id,),
            )
            missing_state = scalar(
                cursor,
                "SELECT iam.campaign_access_state('00000000-0000-7000-8000-999999999999', false)",
            )
            check(
                "API access state distinguishes allowed, forbidden, and missing",
                (
                    viewer_read_state,
                    viewer_edit_state,
                    unauthorized_state,
                    missing_state,
                )
                == ("ALLOWED", "FORBIDDEN", "FORBIDDEN", "NOT_FOUND"),
                (
                    viewer_read_state,
                    viewer_edit_state,
                    unauthorized_state,
                    missing_state,
                ),
            )

            cursor.execute(
                "SELECT iam.set_request_context(%s, 'verify-mtbf-config', 'database verification')",
                (admin_public_id,),
            )
            overall_scope_id = scalar(cursor, "SELECT id FROM reliability.mtbf_scope WHERE scope_code='OVERALL' AND asset_kind='WHOLE_MACHINE' AND status='PUBLISHED' ORDER BY version DESC LIMIT 1")
            mtbf_method_id = scalar(cursor, "SELECT id FROM reliability.statistics_method WHERE implementation_key='mtbf.poisson.exposure_estimate.v1' AND status='PUBLISHED' ORDER BY version DESC LIMIT 1")
            module_scope_count = scalar(
                cursor,
                "SELECT count(*) FROM reliability.mtbf_scope "
                "WHERE scope_code='OVERALL' AND asset_kind='MODULE' AND status='PUBLISHED'",
            )
            check("module OVERALL MTBF scope is independently published", module_scope_count == 1, module_scope_count)
            cursor.execute(
                "INSERT INTO reliability.campaign_mtbf_config(campaign_id,scope_id,method_id,target_seconds,created_by,updated_by) VALUES (%s,%s,%s,3600000,%s,%s)",
                (whole_campaign, overall_scope_id, mtbf_method_id, admin_id, admin_id),
            )

            assert_raises(
                connection,
                RaiseException,
                "INSERT INTO test.campaign_asset(campaign_id,asset_id) VALUES (%s,%s)",
                (whole_campaign, module_asset),
            )
            check("module cannot join whole-machine campaign", True)

            config_id = scalar(cursor, "INSERT INTO test.configuration_snapshot(asset_id,fingerprint,captured_from,effective_from) VALUES (%s,'verify-config','verification',now()) RETURNING id", (whole_asset,))
            test_case_id = scalar(cursor, "INSERT INTO catalog.test_case(case_code,name,asset_kind,target_part_code,domain,evidence_type) VALUES ('VERIFY-CASE','Verify Case','WHOLE_MACHINE','SYS','RELIABILITY','MTBF') RETURNING id")
            test_case_version_id = scalar(cursor, "INSERT INTO catalog.test_case_version(test_case_id,version,procedure_spec,stage_definitions,status) VALUES (%s,'1.0','{}','[]','PUBLISHED') RETURNING id", (test_case_id,))
            cycle_id = scalar(cursor, "INSERT INTO test.test_cycle(cycle_code,campaign_id,asset_id,status,started_at) VALUES ('VERIFY-CYCLE',%s,%s,'ACTIVE',now()) RETURNING id", (whole_campaign, whole_asset))
            segment_id = scalar(cursor, "INSERT INTO test.analysis_segment(campaign_id,cycle_id,configuration_snapshot_id,segment_no,reason,started_at) VALUES (%s,%s,%s,1,'ORIGINAL',now()) RETURNING id", (whole_campaign, cycle_id, config_id))
            execution_id = scalar(cursor, "INSERT INTO test.test_execution(execution_code,campaign_id,cycle_id,asset_id,configuration_snapshot_id,test_case_version_id,station_id,status,normalized_started_at) VALUES ('VERIFY-EXEC',%s,%s,%s,%s,%s,%s,'RUNNING',now()) RETURNING id", (whole_campaign, cycle_id, whole_asset, config_id, test_case_version_id, station_id))
            module_config_id = scalar(cursor, "INSERT INTO test.configuration_snapshot(asset_id,fingerprint,captured_from,effective_from) VALUES (%s,'verify-module-config','verification',now()) RETURNING id", (module_asset,))
            module_cycle_id = scalar(cursor, "INSERT INTO test.test_cycle(cycle_code,campaign_id,asset_id,status,started_at) VALUES ('VERIFY-MODULE-CYCLE',%s,%s,'ACTIVE',now()) RETURNING id", (module_campaign, module_asset))
            arm_case_id = scalar(cursor, "INSERT INTO catalog.test_case(case_code,name,asset_kind,target_part_code,domain,evidence_type) VALUES ('VERIFY-ARM-CASE','Verify Arm Case','MODULE','SARM','RELIABILITY','MTBF') RETURNING id")
            arm_version_id = scalar(cursor, "INSERT INTO catalog.test_case_version(test_case_id,version,procedure_spec,stage_definitions,status) VALUES (%s,'1.0','{}','[]','PUBLISHED') RETURNING id", (arm_case_id,))
            assert_raises(
                connection,
                RaiseException,
                "INSERT INTO test.test_execution(execution_code,campaign_id,cycle_id,asset_id,configuration_snapshot_id,test_case_version_id,station_id,status) VALUES ('VERIFY-MISMATCH-EXEC',%s,%s,%s,%s,%s,%s,'SCHEDULED')",
                (
                    module_campaign,
                    module_cycle_id,
                    module_asset,
                    module_config_id,
                    arm_version_id,
                    station_id,
                ),
            )
            check("leg module cannot execute an arm test case", True)
            stage_id = scalar(cursor, "INSERT INTO test.execution_stage(campaign_id,execution_id,stage_code,stage_name,sequence_no,status,normalized_started_at) VALUES (%s,%s,'LOAD-WALK','Load walk',1,'RUNNING',now()) RETURNING id", (whole_campaign, execution_id))
            stage_started_at = scalar(
                cursor,
                "SELECT normalized_started_at FROM test.execution_stage WHERE id=%s",
                (stage_id,),
            )
            system_source_id = scalar(
                cursor,
                "SELECT id FROM integration.ingestion_source WHERE source_code='SYSTEM'",
            )
            target_position_metric_id = scalar(
                cursor,
                "SELECT version.id FROM catalog.metric_version version "
                "JOIN catalog.metric_definition definition "
                "ON definition.id=version.metric_definition_id "
                "WHERE definition.metric_code='JOINT-TARGET-POSITION' "
                "AND version.version='1.0.0' AND version.status='PUBLISHED'",
            )
            actual_position_metric_id = scalar(
                cursor,
                "SELECT version.id FROM catalog.metric_version version "
                "JOIN catalog.metric_definition definition "
                "ON definition.id=version.metric_definition_id "
                "WHERE definition.metric_code='JOINT-ACTUAL-POSITION' "
                "AND version.version='1.0.0' AND version.status='PUBLISHED'",
            )
            telemetry_series_id = scalar(
                cursor,
                """
                INSERT INTO health.metric_series(
                    campaign_id,asset_id,cycle_id,segment_id,execution_id,
                    stage_id,configuration_snapshot_id,metric_version_id,
                    subject_code,series_kind,cycle_index,started_at,ended_at,
                    point_count,sampling_interval_ms,downsample_method,
                    quality_status,source_id,source_series_key
                ) VALUES (
                    %s,%s,%s,%s,%s,%s,%s,%s,
                    'SYS-HIP-PITCH-L','MOTION_CYCLE',1,%s,%s + interval '1 second',
                    1,1000,'VERIFY_UNIFORM','VALID',%s,'VERIFY-SERIES-001'
                )
                RETURNING id
                """,
                (
                    whole_campaign,
                    whole_asset,
                    cycle_id,
                    segment_id,
                    execution_id,
                    stage_id,
                    config_id,
                    target_position_metric_id,
                    stage_started_at,
                    stage_started_at,
                    system_source_id,
                ),
            )
            assert_raises(
                connection,
                UniqueViolation,
                """
                INSERT INTO health.metric_series(
                    campaign_id,asset_id,cycle_id,segment_id,execution_id,
                    stage_id,configuration_snapshot_id,metric_version_id,
                    subject_code,series_kind,cycle_index,started_at,ended_at,
                    point_count,sampling_interval_ms,downsample_method,
                    quality_status,source_id,source_series_key
                ) VALUES (
                    %s,%s,%s,%s,%s,%s,%s,%s,
                    'SYS-HIP-PITCH-L','MOTION_CYCLE',1,%s,%s + interval '1 second',
                    1,1000,'VERIFY_UNIFORM','VALID',%s,'VERIFY-SERIES-001'
                )
                """,
                (
                    whole_campaign,
                    whole_asset,
                    cycle_id,
                    segment_id,
                    execution_id,
                    stage_id,
                    config_id,
                    target_position_metric_id,
                    stage_started_at,
                    stage_started_at,
                    system_source_id,
                ),
            )
            check("series source idempotency key rejects duplicates", True)
            telemetry_observation_id = scalar(
                cursor,
                """
                INSERT INTO health.metric_observation(
                    campaign_id,metric_version_id,asset_id,cycle_id,segment_id,
                    execution_id,stage_id,configuration_snapshot_id,
                    series_id,sample_index,observed_at,canonical_numeric_value,
                    original_numeric_value,original_unit,quality_status,source_kind
                ) VALUES (
                    %s,%s,%s,%s,%s,%s,%s,%s,%s,0,%s,
                    1.25,1.25,'deg','VALID','NATIVE'
                )
                RETURNING id
                """,
                (
                    whole_campaign,
                    target_position_metric_id,
                    whole_asset,
                    cycle_id,
                    segment_id,
                    execution_id,
                    stage_id,
                    config_id,
                    telemetry_series_id,
                    stage_started_at,
                ),
            )
            check(
                "series-linked telemetry observation is accepted",
                telemetry_observation_id is not None,
                telemetry_observation_id,
            )
            assert_raises(
                connection,
                UniqueViolation,
                """
                INSERT INTO health.metric_observation(
                    campaign_id,metric_version_id,asset_id,cycle_id,segment_id,
                    execution_id,stage_id,configuration_snapshot_id,
                    series_id,sample_index,observed_at,canonical_numeric_value,
                    original_numeric_value,original_unit,quality_status,source_kind
                ) VALUES (
                    %s,%s,%s,%s,%s,%s,%s,%s,%s,0,%s,
                    1.30,1.30,'deg','VALID','NATIVE'
                )
                """,
                (
                    whole_campaign,
                    target_position_metric_id,
                    whole_asset,
                    cycle_id,
                    segment_id,
                    execution_id,
                    stage_id,
                    config_id,
                    telemetry_series_id,
                    stage_started_at,
                ),
            )
            check("sample index is unique inside a metric series", True)
            assert_raises(
                connection,
                RaiseException,
                """
                INSERT INTO health.metric_observation(
                    campaign_id,metric_version_id,asset_id,cycle_id,segment_id,
                    execution_id,stage_id,configuration_snapshot_id,
                    series_id,sample_index,observed_at,canonical_numeric_value,
                    original_numeric_value,original_unit,quality_status,source_kind
                ) VALUES (
                    %s,%s,%s,%s,%s,%s,%s,%s,%s,1,%s + interval '0.5 second',
                    1.35,1.35,'deg','VALID','NATIVE'
                )
                """,
                (
                    whole_campaign,
                    actual_position_metric_id,
                    whole_asset,
                    cycle_id,
                    segment_id,
                    execution_id,
                    stage_id,
                    config_id,
                    telemetry_series_id,
                    stage_started_at,
                ),
            )
            check("series trigger rejects a mismatched metric point", True)
            native_interval = scalar(cursor, "INSERT INTO test.runtime_interval(campaign_id,asset_id,cycle_id,execution_id,stage_id,source_kind,started_at,ended_at,active_seconds,clock_quality,data_quality,source_method) VALUES (%s,%s,%s,%s,%s,'NATIVE',now()-interval '2 hours',now()-interval '1 hour',3600,'VALID','VALID','collector') RETURNING id", (whole_campaign, whole_asset, cycle_id, execution_id, stage_id))

            assert_raises(
                connection,
                ExclusionViolation,
                "INSERT INTO test.runtime_interval(campaign_id,asset_id,cycle_id,execution_id,stage_id,source_kind,started_at,ended_at,active_seconds,clock_quality,data_quality,source_method) VALUES (%s,%s,%s,%s,%s,'NATIVE',now()-interval '90 minutes',now()-interval '30 minutes',3600,'VALID','VALID','collector')",
                (whole_campaign, whole_asset, cycle_id, execution_id, stage_id),
            )
            check("overlapping native runtime is rejected", True)

            legacy_interval = scalar(cursor, "INSERT INTO test.runtime_interval(campaign_id,asset_id,source_kind,active_seconds,clock_quality,data_quality,source_method) VALUES (%s,%s,'LEGACY_REPORTED',7200,'PARTIAL','PARTIAL','legacy-total') RETURNING id", (whole_campaign, whole_asset))
            assert_raises(
                connection,
                RaiseException,
                "INSERT INTO reliability.exposure_assessment(campaign_id,asset_id,runtime_interval_id,configuration_snapshot_id,eligible,assignment_status,quality_status,assessment_method) VALUES (%s,%s,%s,%s,true,'CONFIRMED','PARTIAL','verify')",
                (whole_campaign, whole_asset, legacy_interval, config_id),
            )
            check("legacy test time cannot enter MTBF exposure", True)

            total_seconds = scalar(cursor, "SELECT total_test_seconds FROM test.asset_test_time_summary WHERE asset_id=%s", (whole_asset,))
            check("unified test time includes legacy and native", total_seconds == 10800, str(total_seconds))

            error_catalog_id = scalar(cursor, "INSERT INTO catalog.error_catalog_version(version,status) VALUES ('verify-errors','DRAFT') RETURNING id")
            error_code_id = scalar(cursor, "INSERT INTO catalog.error_code(catalog_version_id,unified_code,domain,subsystem,category,title) VALUES (%s,'VERIFY-ERR','HARDWARE','JOINT','MOTOR','Verify error') RETURNING id", (error_catalog_id,))
            event_id = scalar(cursor, "INSERT INTO test.test_event(campaign_id,asset_id,cycle_id,execution_id,stage_id,event_type,received_at,normalized_time,clock_quality,data_quality,payload) VALUES (%s,%s,%s,%s,%s,'INTERRUPTION',now(),now(),'VALID','VALID','{}') RETURNING id", (whole_campaign, whole_asset, cycle_id, execution_id, stage_id))
            assert_raises(
                connection,
                RaiseException,
                "INSERT INTO reliability.interruption(campaign_id,asset_id,event_id,classification,review_status,primary_error_code_id,started_at) VALUES (%s,%s,%s,'FAILED','CONFIRMED',%s,now())",
                (whole_campaign, whole_asset, event_id, error_code_id),
            )
            cursor.execute("UPDATE catalog.error_catalog_version SET status='PUBLISHED',published_at=now() WHERE id=%s", (error_catalog_id,))
            interruption_id = scalar(cursor, "INSERT INTO reliability.interruption(campaign_id,asset_id,event_id,classification,review_status,primary_error_code_id,started_at) VALUES (%s,%s,%s,'FAILED','CONFIRMED',%s,now()) RETURNING id", (whole_campaign, whole_asset, event_id, error_code_id))
            check("FAILED accepts published formal error code", interruption_id is not None, interruption_id)

            # RLS: viewer reads authorized campaign but cannot update; executor can update.
            cursor.execute(sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(os.environ.get("RP1_APP_USER", "rp1_app"))))
            cursor.execute("SELECT set_config('app.user_id', %s, true)", (str(viewer_id),))
            visible = scalar(cursor, "SELECT count(*) FROM test.test_campaign")
            check("viewer sees only authorized campaign", visible == 1, visible)
            cursor.execute("UPDATE test.asset SET notes='viewer-write' WHERE id=%s", (whole_asset,))
            check("viewer cannot update asset", cursor.rowcount == 0, cursor.rowcount)
            cursor.execute("SELECT set_config('app.user_id', %s, true)", (str(executor_id),))
            cursor.execute("SELECT set_config('app.request_id', 'verify-rls', true)")
            cursor.execute("SELECT set_config('app.change_reason', 'database verification', true)")
            cursor.execute("UPDATE test.asset SET notes='executor-write' WHERE id=%s", (whole_asset,))
            check("executor can update authorized asset", cursor.rowcount == 1, cursor.rowcount)
            cursor.execute("RESET ROLE")

            audit_rows = scalar(cursor, "SELECT count(*) FROM audit.change_log WHERE request_id='verify-rls' AND table_name='asset' AND operation='UPDATE'")
            check("business update stores before/after audit", audit_rows == 1, audit_rows)
            queued = scalar(cursor, "SELECT count(*) FROM reliability.recompute_job WHERE status='PENDING'")
            check("fact changes coalesce into recomputation", queued >= 1, queued)

            cursor.execute(sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(os.environ.get("RP1_APP_USER", "rp1_app"))))
            cursor.execute("SELECT set_config('app.user_id', %s, true)", (str(executor_id),))
            claimed_job = scalar(cursor, "SELECT id FROM reliability.claim_recompute_job('verify-worker')")
            cursor.execute("RESET ROLE")
            check("worker claims one recompute job safely", claimed_job is not None, claimed_job)

            cursor.execute(sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(os.environ.get("RP1_APP_USER", "rp1_app"))))
            cursor.execute("SELECT set_config('app.user_id', %s, true)", (str(executor_id),))
            assert_raises(
                connection,
                InsufficientPrivilege,
                "DELETE FROM audit.change_log WHERE request_id='verify-rls'",
            )
            cursor.execute("RESET ROLE")
            check("application role cannot delete audit records", True)

        connection.rollback()

    print(json.dumps({"summary": {"total": len(results), "passed": len(results), "failed": 0}, "results": results}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({"summary": {"failed": 1}, "error": repr(exc)}, ensure_ascii=False, indent=2), file=sys.stderr)
        raise
