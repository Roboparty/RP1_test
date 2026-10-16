from __future__ import annotations

import csv
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from sqlalchemy import text


EXPECTED_HEADERS = [
    "id",
    "name",
    "module",
    "priority",
    "preconditions",
    "expected_result",
    "pass_criteria",
    "owner",
    "enabled",
    "source",
    "created_at",
    "updated_at",
]

PREFIX_MAPPING = {
    "SYS-REL": ("WHOLE_MACHINE", "SYS", "SYS 整机"),
    "REL-SARM": ("MODULE", "SARM", "MOD 模块"),
    "REL-SLEG": ("MODULE", "SLEG", "MOD 模块"),
    "REL-CHEST": ("MODULE", "CHEST", "MOD 模块"),
    "REL-UPPER": ("MODULE", "UPPER", "MOD 模块"),
    "REL-LOWER": ("MODULE", "LOWER", "MOD 模块"),
}

EXPECTED_COUNTS = {
    "SYS-REL": 11,
    "REL-SARM": 10,
    "REL-SLEG": 10,
    "REL-CHEST": 9,
    "REL-UPPER": 10,
    "REL-LOWER": 10,
}


def _parse_timestamp(value: str) -> datetime:
    normalized = value
    if len(value) >= 3 and value[-3] in {"+", "-"} and value[-2:].isdigit():
        normalized = f"{value}:00"
    return datetime.fromisoformat(normalized)


def _mapping_for(case_code: str) -> tuple[str, str, str, str]:
    matches = [
        (prefix, values)
        for prefix, values in PREFIX_MAPPING.items()
        if case_code.startswith(f"{prefix}-")
    ]
    if len(matches) != 1:
        raise ValueError(f"unknown or ambiguous test case prefix: {case_code}")
    prefix, (asset_kind, target_part_code, module) = matches[0]
    suffix = case_code.removeprefix(f"{prefix}-")
    if len(suffix) != 3 or not suffix.isdigit():
        raise ValueError(f"invalid test case id format: {case_code}")
    return prefix, asset_kind, target_part_code, module


def validate_rows(fieldnames: list[str] | None, rows: Iterable[dict[str, str]]) -> list[dict[str, Any]]:
    if fieldnames != EXPECTED_HEADERS:
        raise ValueError(f"unexpected CSV headers: {fieldnames!r}")

    parsed: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    counts: Counter[str] = Counter()
    for line_number, row in enumerate(rows, start=2):
        case_code = (row.get("id") or "").strip()
        if not case_code or case_code in seen_ids:
            raise ValueError(f"missing or duplicate id at CSV line {line_number}: {case_code!r}")
        seen_ids.add(case_code)
        prefix, asset_kind, target_part_code, expected_module = _mapping_for(case_code)
        if row["module"] != expected_module:
            raise ValueError(
                f"module mismatch at CSV line {line_number}: "
                f"{row['module']!r} != {expected_module!r}"
            )
        if row["enabled"] != "1":
            raise ValueError(f"enabled must be 1 at CSV line {line_number}")
        if not row["name"].strip():
            raise ValueError(f"name is required at CSV line {line_number}")
        if not row["priority"].strip():
            raise ValueError(f"priority is required at CSV line {line_number}")
        try:
            created_at = _parse_timestamp(row["created_at"])
            updated_at = _parse_timestamp(row["updated_at"])
        except ValueError as exc:
            raise ValueError(f"invalid timestamp at CSV line {line_number}") from exc
        if created_at.tzinfo is None or updated_at.tzinfo is None:
            raise ValueError(f"timestamps must include timezone at CSV line {line_number}")
        if updated_at < created_at:
            raise ValueError(f"updated_at precedes created_at at CSV line {line_number}")

        counts[prefix] += 1
        parsed.append(
            {
                **row,
                "case_code": case_code,
                "asset_kind": asset_kind,
                "target_part_code": target_part_code,
                "created_at_value": created_at,
                "updated_at_value": updated_at,
            }
        )

    if len(parsed) != 60:
        raise ValueError(f"expected exactly 60 test cases, got {len(parsed)}")
    if dict(counts) != EXPECTED_COUNTS:
        raise ValueError(f"unexpected prefix counts: {dict(counts)!r}")
    return parsed


def load_csv(csv_path: Path) -> list[dict[str, Any]]:
    with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        return validate_rows(reader.fieldnames, reader)


def import_test_cases(connection: Any, csv_path: Path) -> None:
    rows = load_csv(csv_path)
    case_statement = text(
        """
        INSERT INTO catalog.test_case(
            case_code, name, asset_kind, target_part_code, domain,
            evidence_type, enabled, created_at, updated_at
        ) VALUES (
            :case_code, :name, :asset_kind, :target_part_code, 'RELIABILITY',
            'RUNTIME_AND_METRIC', true, :created_at_value, :updated_at_value
        )
        ON CONFLICT (case_code) DO UPDATE SET
            name = EXCLUDED.name,
            asset_kind = EXCLUDED.asset_kind,
            target_part_code = EXCLUDED.target_part_code,
            domain = EXCLUDED.domain,
            evidence_type = EXCLUDED.evidence_type,
            enabled = EXCLUDED.enabled,
            created_at = EXCLUDED.created_at,
            updated_at = EXCLUDED.updated_at
        RETURNING id
        """
    )
    version_statement = text(
        """
        INSERT INTO catalog.test_case_version(
            test_case_id, version, procedure_spec, stage_definitions,
            equipment_requirements, metric_requirements, sampling_requirements,
            fault_rules, termination_rules, decision_rules, source_references,
            deviation_notes, status, published_by, published_at,
            created_at, updated_at
        ) VALUES (
            :test_case_id, 'CSV-20260918',
            jsonb_build_object(
                'priority', CAST(:priority AS text),
                'preconditions', CAST(:preconditions AS text),
                'expected_result', CAST(:expected_result AS text),
                'owner', CAST(:owner AS text),
                'source', CAST(:source AS text)
            ),
            '[]'::jsonb, '{}'::jsonb, '[]'::jsonb, '{}'::jsonb,
            '{}'::jsonb, '{}'::jsonb,
            jsonb_build_object('pass_criteria', CAST(:pass_criteria AS text)),
            jsonb_build_array(
                jsonb_build_object(
                    'file', 'test_cases_202609181013.csv',
                    'source', CAST(:source AS text)
                )
            ),
            '', 'PUBLISHED', NULL, :updated_at_value,
            :created_at_value, :updated_at_value
        )
        ON CONFLICT (test_case_id, version) DO UPDATE SET
            procedure_spec = EXCLUDED.procedure_spec,
            stage_definitions = EXCLUDED.stage_definitions,
            equipment_requirements = EXCLUDED.equipment_requirements,
            metric_requirements = EXCLUDED.metric_requirements,
            sampling_requirements = EXCLUDED.sampling_requirements,
            fault_rules = EXCLUDED.fault_rules,
            termination_rules = EXCLUDED.termination_rules,
            decision_rules = EXCLUDED.decision_rules,
            source_references = EXCLUDED.source_references,
            deviation_notes = EXCLUDED.deviation_notes,
            status = EXCLUDED.status,
            published_by = NULL,
            published_at = EXCLUDED.published_at,
            created_at = EXCLUDED.created_at,
            updated_at = EXCLUDED.updated_at
        """
    )
    for row in rows:
        test_case_id = connection.execute(case_statement, row).scalar_one()
        connection.execute(version_statement, {**row, "test_case_id": test_case_id})
