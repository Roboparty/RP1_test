from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from sqlalchemy import text

from imports.whole_machine_catalog import VERSION as SOURCE_VERSION
from imports.whole_machine_catalog import load_catalog

REVISION = "DOCX-V1.0-R1-20260921"
REVISED_CASE_CODE = "SYS-REL-002"
DISABLED_CASE_CODE = "SYS-REL-015"


def load_revision(source_path: Path, revision_path: Path) -> dict[str, Any]:
    source = load_catalog(source_path)
    revision = json.loads(revision_path.read_text(encoding="utf-8"))
    if revision.get("schema_version") != "1.0":
        raise ValueError("unsupported whole-machine revision schema")
    if revision.get("revision") != REVISION:
        raise ValueError("unexpected whole-machine revision")
    if revision.get("source_version") != SOURCE_VERSION:
        raise ValueError("whole-machine revision has an unexpected source version")

    changes = revision.get("changes")
    if not isinstance(changes, list):
        raise TypeError("whole-machine revision changes must be a list")
    changes_by_code = {item.get("case_code"): item for item in changes}
    if set(changes_by_code) != {REVISED_CASE_CODE, DISABLED_CASE_CODE}:
        raise ValueError(
            "whole-machine revision must address SYS-REL-002 and SYS-REL-015"
        )
    if changes_by_code[REVISED_CASE_CODE].get("action") != "PUBLISH_REVISED":
        raise ValueError("SYS-REL-002 must be published as a revised version")
    if changes_by_code[DISABLED_CASE_CODE].get("action") != "DISABLE":
        raise ValueError("SYS-REL-015 must be disabled")

    source_by_code = {item["case_code"]: item for item in source["cases"]}
    revised_case = copy.deepcopy(source_by_code[REVISED_CASE_CODE])
    change = changes_by_code[REVISED_CASE_CODE]
    old_step = change["replace_step"]["from"]
    new_step = change["replace_step"]["to"]
    steps = revised_case["procedure_spec"]["steps"]
    if steps.count(old_step) != 1:
        raise ValueError(
            "SYS-REL-002 source step no longer matches the approved revision"
        )
    revised_case["procedure_spec"]["steps"] = [
        new_step if step == old_step else step for step in steps
    ]
    revised_case["procedure_spec"]["matrix"]["primary_stress_or_duration"] = change[
        "primary_stress_or_duration"
    ]
    revised_case["procedure_spec"]["basis_and_boundary"] = change["basis_and_boundary"]
    revised_case["version"] = REVISION
    revised_case["status"] = "PUBLISHED"
    revised_case["deviation_notes"] = []

    if "累计50 km及300次姿态循环" not in change["primary_stress_or_duration"]:
        raise ValueError("SYS-REL-002 matrix does not contain the approved target")
    if "每5 km执行30次" not in new_step or "不少于300次" not in new_step:
        raise ValueError("SYS-REL-002 step does not contain the approved cycle rule")
    if not revision.get("approved_at"):
        raise ValueError("whole-machine revision has no approval timestamp")
    if any(not item.get("approval_note") for item in changes):
        raise ValueError("every whole-machine revision change needs an approval note")

    return {
        "source": source["source"],
        "source_catalog": source,
        "revision": revision,
        "revised_case": revised_case,
    }


def import_revision(
    connection: Any,
    source_path: Path,
    revision_path: Path,
) -> None:
    payload = load_revision(source_path, revision_path)
    source = payload["source"]
    revision = payload["revision"]
    item = payload["revised_case"]
    approved_at = revision["approved_at"]

    test_case_id = connection.execute(
        text(
            """
            SELECT id
            FROM catalog.test_case
            WHERE case_code = :case_code
            """
        ),
        {"case_code": REVISED_CASE_CODE},
    ).scalar_one()
    source_references = [
        {
            "file": source["file"],
            "sha256": source["sha256"],
            "document_version": source["document_version"],
        },
        {
            "type": "APPROVED_OVERRIDE",
            "approved_at": approved_at,
            "approval_note": next(
                change["approval_note"]
                for change in revision["changes"]
                if change["case_code"] == REVISED_CASE_CODE
            ),
        },
    ]
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
        text(
            """
            INSERT INTO catalog.test_case_version(
                test_case_id, version, procedure_spec, stage_definitions,
                equipment_requirements, metric_requirements,
                sampling_requirements, fault_rules, termination_rules,
                decision_rules, source_references, deviation_notes, status,
                published_by, published_at, created_at, updated_at
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
                '', 'PUBLISHED', NULL,
                CAST(:approved_at AS timestamptz),
                CAST(:approved_at AS timestamptz),
                CAST(:approved_at AS timestamptz)
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
        ),
        {
            **json_values,
            "test_case_id": test_case_id,
            "version": REVISION,
            "source_references": json.dumps(source_references, ensure_ascii=False),
            "approved_at": approved_at,
        },
    )
    disabled = connection.execute(
        text(
            """
            UPDATE catalog.test_case
            SET enabled = false,
                updated_at = CAST(:approved_at AS timestamptz)
            WHERE case_code = :case_code
            """
        ),
        {
            "case_code": DISABLED_CASE_CODE,
            "approved_at": approved_at,
        },
    ).rowcount
    if disabled != 1:
        raise RuntimeError("SYS-REL-015 was not disabled exactly once")
