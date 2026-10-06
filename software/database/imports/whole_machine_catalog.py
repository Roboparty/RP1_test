from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from sqlalchemy import text

VERSION = "DOCX-V1.0-20260921"
EXPECTED_CODES = {
    *(f"SYS-REL-{number:03d}" for number in range(1, 18)),
    "SYS-REL-020",
    "SYS-REL-021",
}
EXPECTED_DRAFT_CODES = {"SYS-REL-002", "SYS-REL-015"}
EXPECTED_SOURCE_SHA256 = (
    "66e211d8cad1296539ca8c00c4b0b342da6a87affb51ad97ec13c225b99502d1"
)


def load_catalog(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != "1.0":
        raise ValueError("unsupported whole-machine catalog schema")
    source = payload.get("source") or {}
    if source.get("sha256") != EXPECTED_SOURCE_SHA256:
        raise ValueError("whole-machine source SHA-256 does not match approved V1.0")

    cases = payload.get("cases")
    if not isinstance(cases, list):
        raise TypeError("whole-machine cases must be a list")
    codes = [item.get("case_code") for item in cases]
    if len(codes) != len(set(codes)):
        raise ValueError("whole-machine case codes must be unique")
    if set(codes) != EXPECTED_CODES:
        raise ValueError(f"unexpected whole-machine case codes: {codes!r}")

    draft_codes = {item["case_code"] for item in cases if item.get("status") == "DRAFT"}
    if draft_codes != EXPECTED_DRAFT_CODES:
        raise ValueError(f"unexpected draft cases: {sorted(draft_codes)!r}")
    for item in cases:
        if item.get("version") != VERSION:
            raise ValueError(f"{item['case_code']} has an unexpected version")
        if item.get("asset_kind") != "WHOLE_MACHINE":
            raise ValueError(f"{item['case_code']} must target WHOLE_MACHINE")
        if item.get("target_part_code") != "SYS":
            raise ValueError(f"{item['case_code']} must target SYS")
        if not item.get("name"):
            raise ValueError(f"{item['case_code']} has no name")
        if item.get("status") == "PUBLISHED":
            procedure = item.get("procedure_spec") or {}
            required = (
                "preconditions",
                "steps",
                "monitoring_and_records",
            )
            if any(not procedure.get(key) for key in required):
                raise ValueError(
                    f"{item['case_code']} published procedure is incomplete"
                )
            if not (item.get("decision_rules") or {}).get("pass_criteria"):
                raise ValueError(
                    f"{item['case_code']} published decision rules are incomplete"
                )
            if not (item.get("termination_rules") or {}).get("items"):
                raise ValueError(
                    f"{item['case_code']} published termination rules are incomplete"
                )
        elif item.get("status") != "DRAFT":
            raise ValueError(f"{item['case_code']} has an invalid status")
        elif not item.get("deviation_notes"):
            raise ValueError(f"{item['case_code']} draft has no blocking note")
    return payload


def import_catalog(connection: Any, path: Path) -> None:
    payload = load_catalog(path)
    source = payload["source"]
    case_statement = text(
        """
        INSERT INTO catalog.test_case(
            case_code, name, asset_kind, target_part_code, domain,
            evidence_type, enabled, created_at, updated_at
        ) VALUES (
            :case_code, :name, 'WHOLE_MACHINE', 'SYS', 'RELIABILITY',
            'RUNTIME_AND_METRIC', true, :imported_at, :imported_at
        )
        ON CONFLICT (case_code) DO UPDATE SET
            name = EXCLUDED.name,
            asset_kind = EXCLUDED.asset_kind,
            target_part_code = EXCLUDED.target_part_code,
            domain = EXCLUDED.domain,
            evidence_type = EXCLUDED.evidence_type,
            enabled = EXCLUDED.enabled,
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
            :test_case_id, :version,
            CAST(:procedure_spec AS jsonb),
            CAST(:stage_definitions AS jsonb),
            CAST(:equipment_requirements AS jsonb),
            CAST(:metric_requirements AS jsonb),
            CAST(:sampling_requirements AS jsonb),
            CAST(:fault_rules AS jsonb),
            CAST(:termination_rules AS jsonb),
            CAST(:decision_rules AS jsonb),
            CAST(:source_references AS jsonb),
            :deviation_notes, :status, NULL,
            CASE WHEN :status = 'PUBLISHED' THEN CAST(:imported_at AS timestamptz)
                 ELSE NULL END,
            CAST(:imported_at AS timestamptz),
            CAST(:imported_at AS timestamptz)
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
            updated_at = EXCLUDED.updated_at
        """
    )
    source_reference = [
        {
            "file": source["file"],
            "sha256": source["sha256"],
            "document_version": source["document_version"],
        }
    ]
    for item in payload["cases"]:
        test_case_id = connection.execute(
            case_statement,
            {
                "case_code": item["case_code"],
                "name": item["name"],
                "imported_at": source["imported_at"],
            },
        ).scalar_one()
        json_values = {
            field: json.dumps(item[field], ensure_ascii=False)
            for field in (
                "procedure_spec",
                "stage_definitions",
                "equipment_requirements",
                "metric_requirements",
                "sampling_requirements",
                "fault_rules",
                "termination_rules",
                "decision_rules",
            )
        }
        connection.execute(
            version_statement,
            {
                **json_values,
                "test_case_id": test_case_id,
                "version": VERSION,
                "source_references": json.dumps(source_reference, ensure_ascii=False),
                "deviation_notes": "\n".join(item["deviation_notes"]),
                "status": item["status"],
                "imported_at": source["imported_at"],
            },
        )
