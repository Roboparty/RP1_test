from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
from collections import Counter, defaultdict
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable

from sqlalchemy import create_engine, text


SOURCE_CODE = "LEGACY-EXECUTION-CSV-20260918"
SOURCE_FILE = "executions_202609181314.csv"
IMPORT_BATCH_CODE = "EXECUTION-CSV-20260918"
EXPECTED_ROW_COUNT = 135
EXPECTED_HEADERS = [
    "test_id",
    "test_case_id",
    "test_case_name",
    "sample_id",
    "bench_id",
    "robot_id",
    "status",
    "started_at",
    "ended_at",
    "duration_hours",
    "duration_display",
    "executor",
    "environment",
    "summary",
    "issues",
    "exception_count",
    "last_data_at",
    "report_url",
    "telemetry_source",
    "archive_status",
    "archive_path",
    "created_at",
    "updated_at",
]
EXPECTED_STATUS_COUNTS = Counter(
    {"passed": 89, "failed": 17, "blocked": 18, "scheduled": 9, "running": 2}
)
EXPECTED_BENCH_COUNTS = Counter({"RD-01": 30, "RD-02": 72, "RD-03": 33})
EXPECTED_CASE_COUNTS = Counter(
    {
        "REL-SARM-001": 6,
        "REL-SARM-002": 18,
        "REL-SLEG-001": 5,
        "REL-SLEG-002": 9,
        "REL-SLEG-010": 6,
        "REL-UPPER-001": 43,
        "REL-LOWER-001": 32,
        "REL-LOWER-002": 11,
        "PERF-SARM-001": 5,
    }
)
CASE_PATTERN = re.compile(
    r"^(?P<domain>REL|PERF)-(?P<part>SARM|SLEG|UPPER|LOWER)-(?P<number>[0-9]{3})$"
)
SOURCE_ASSET_PATTERN = re.compile(
    r"^RP1[.]3-(?P<part>ARM|SARM|LEG|SLEG|UPPER|LOWER)-"
    r"(?P<sequence>[Oo]?[0-9]{1,3})$",
    re.IGNORECASE,
)
SOURCE_KEY_PATTERN = re.compile(r"^[A-Z0-9]+(?:-[A-Z0-9]+)*_[0-9]{10}_.+_RD-[0-9]{2}$")
FINAL_STATUSES = {"passed", "failed", "blocked"}
ACTIVE_STATUSES = {"scheduled", "running"}
ARCHIVE_MAPPING = {
    "pending": "PENDING",
    "imported": "IMPORTED",
    "archived": "ARCHIVED",
    "": "MISSING",
}


def _parse_timestamp(
    value: str,
    *,
    field: str,
    line_number: int,
    required: bool = True,
) -> datetime | None:
    raw = value.strip()
    if not raw:
        if required:
            raise ValueError(f"{field} is required at CSV line {line_number}")
        return None
    if (
        len(raw) >= 3
        and raw[-3] in {"+", "-"}
        and raw[-2:].isdigit()
    ):
        raw = f"{raw}:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError(
            f"invalid {field} timestamp at CSV line {line_number}: {value!r}"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(
            f"{field} must include a timezone at CSV line {line_number}"
        )
    return parsed


def _parse_decimal(value: str, *, field: str, line_number: int) -> Decimal:
    try:
        parsed = Decimal(value.strip())
    except (InvalidOperation, AttributeError) as exc:
        raise ValueError(
            f"invalid {field} at CSV line {line_number}: {value!r}"
        ) from exc
    if not parsed.is_finite() or parsed < 0:
        raise ValueError(
            f"{field} must be finite and non-negative at CSV line {line_number}"
        )
    return parsed


def case_target_part(case_code: str) -> str:
    match = CASE_PATTERN.fullmatch(case_code)
    if match is None:
        raise ValueError(f"invalid execution test case format: {case_code!r}")
    return match.group("part")


def _source_asset_parts(source_asset_code: str) -> tuple[str, str, int]:
    match = SOURCE_ASSET_PATTERN.fullmatch(source_asset_code.strip())
    if match is None:
        raise ValueError(f"invalid source sample format: {source_asset_code!r}")
    source_part = match.group("part").upper()
    semantic_part = {"ARM": "SARM", "LEG": "SLEG"}.get(source_part, source_part)
    sequence_text = match.group("sequence")
    sequence = int(sequence_text[1:] if sequence_text[:1].upper() == "O" else sequence_text)
    if not 1 <= sequence <= 999:
        raise ValueError(f"invalid source sample sequence: {source_asset_code!r}")
    return source_part, semantic_part, sequence


def _identifier_parts(source_identifier: str) -> tuple[str | None, int]:
    normalized = source_identifier.strip().upper()
    match = re.search(r"([0-9]{1,3})$", normalized)
    if match is None:
        raise ValueError(
            f"source identifier has no trailing sequence: {source_identifier!r}"
        )
    sequence = int(match.group(1))
    if not 1 <= sequence <= 999:
        raise ValueError(
            f"source identifier sequence is out of range: {source_identifier!r}"
        )
    part_match = re.search(
        r"(?:^|[-.])(SARM|ARM|SLEG|LEG|UPPER|LOWER)(?:-|$)",
        normalized,
    )
    semantic_part = None
    if part_match is not None:
        source_part = part_match.group(1)
        semantic_part = {"ARM": "SARM", "LEG": "SLEG"}.get(
            source_part, source_part
        )
    return semantic_part, sequence


def normalize_asset(
    case_code: str,
    sample_id: str,
    robot_id: str,
) -> tuple[str, list[dict[str, Any]]]:
    target_part = case_target_part(case_code)
    source_part, sample_semantic_part, sample_sequence = _source_asset_parts(
        sample_id
    )
    robot_semantic_part, robot_sequence = _identifier_parts(robot_id)
    if sample_semantic_part == target_part:
        selected_sequence = sample_sequence
        selected_sequence_source = "sample_id"
    elif robot_semantic_part == target_part:
        selected_sequence = robot_sequence
        selected_sequence_source = "robot_id"
    else:
        selected_sequence = sample_sequence
        selected_sequence_source = "sample_id_fallback"
    asset_code = f"RP1.3-{target_part}-{selected_sequence:03d}"
    warnings: list[dict[str, Any]] = []

    if source_part != target_part:
        warnings.append(
            {
                "code": "case_part_overrode_source_part",
                "field": "sample_id",
                "source_part": source_part,
                "normalized_part": target_part,
                "rule": "test_case_id determines target part",
            }
        )
    if sample_id.strip() != asset_code:
        warnings.append(
            {
                "code": "asset_code_normalized",
                "field": "sample_id",
                "source_value": sample_id,
                "normalized_value": asset_code,
            }
        )
    if robot_sequence != sample_sequence:
        warnings.append(
            {
                "code": "robot_sample_sequence_conflict",
                "field": "robot_id",
                "source_value": robot_id,
                "sample_sequence": sample_sequence,
                "robot_sequence": robot_sequence,
                "selected_sequence": selected_sequence,
                "resolution": selected_sequence_source,
            }
        )
    return asset_code, warnings


def detect_termination(summary: str, issues: str) -> str:
    semantic_text = f"{summary}\n{issues}".casefold()
    if any(
        marker in semantic_text
        for marker in ("watchdog", "安全监控", "controller_timeout")
    ):
        return "SAFETY_WATCHDOG"
    if any(
        marker in semantic_text
        for marker in ("采集端超时", "超时无数据", "data timeout")
    ):
        return "DATA_TIMEOUT"
    if any(
        marker in semantic_text
        for marker in (
            "卡死",
            "强制关机",
            "异常退出",
            "network is down",
            "no such device",
            "设备于",
        )
    ):
        return "SYSTEM_CRASH"
    if any(
        marker in semantic_text
        for marker in ("ctrl+c", "手动停止", "操作员手动停止")
    ):
        return "MANUAL_STOP"
    return "UNKNOWN"


def normalize_status(
    source_status: str,
    summary: str,
    issues: str,
) -> dict[str, Any]:
    if source_status == "scheduled":
        return {
            "execution_status": "SCHEDULED",
            "outcome": "NOT_EVALUATED",
            "termination_kind": "SCHEDULED",
            "source_status_conflict": False,
        }
    if source_status == "running":
        return {
            "execution_status": "RUNNING",
            "outcome": "NOT_EVALUATED",
            "termination_kind": "RUNNING",
            "source_status_conflict": False,
        }

    termination_kind = detect_termination(summary, issues)
    if source_status == "failed":
        return {
            "execution_status": "FAILED",
            "outcome": "FAILED",
            "termination_kind": termination_kind,
            "source_status_conflict": False,
        }
    if source_status == "blocked":
        return {
            "execution_status": "BLOCKED",
            "outcome": "INCONCLUSIVE",
            "termination_kind": termination_kind,
            "source_status_conflict": False,
        }
    if source_status == "passed":
        if termination_kind == "MANUAL_STOP":
            return {
                "execution_status": "COMPLETED",
                "outcome": "INCONCLUSIVE",
                "termination_kind": termination_kind,
                "source_status_conflict": True,
            }
        if termination_kind != "UNKNOWN":
            return {
                "execution_status": "BLOCKED",
                "outcome": "INCONCLUSIVE",
                "termination_kind": termination_kind,
                "source_status_conflict": True,
            }
        return {
            "execution_status": "COMPLETED",
            "outcome": "PASSED",
            "termination_kind": "NORMAL",
            "source_status_conflict": False,
        }
    raise ValueError(f"unsupported source status: {source_status!r}")


def _seconds_text(value: Decimal) -> str:
    return format(value.quantize(Decimal("0.000001")), "f")


def conservative_active_seconds(
    reported_seconds: Decimal,
    started_at: datetime,
    ended_at: datetime,
    last_data_at: datetime,
) -> tuple[Decimal, str, list[dict[str, Any]]]:
    elapsed_seconds = Decimal(str((ended_at - started_at).total_seconds()))
    last_data_seconds = Decimal(str((last_data_at - started_at).total_seconds()))
    candidates = {
        "reported_duration_seconds": reported_seconds,
        "elapsed_seconds": elapsed_seconds,
        "last_data_window_seconds": last_data_seconds,
    }
    active_seconds = min(candidates.values())
    warnings: list[dict[str, Any]] = []

    if abs(reported_seconds - elapsed_seconds) > Decimal("60"):
        warnings.append(
            {
                "code": "reported_duration_mismatch",
                "reported_seconds": _seconds_text(reported_seconds),
                "elapsed_seconds": _seconds_text(elapsed_seconds),
                "difference_seconds": _seconds_text(
                    abs(reported_seconds - elapsed_seconds)
                ),
            }
        )
    if elapsed_seconds - last_data_seconds > Decimal("60"):
        warnings.append(
            {
                "code": "last_data_window_shorter_than_elapsed",
                "last_data_window_seconds": _seconds_text(last_data_seconds),
                "elapsed_seconds": _seconds_text(elapsed_seconds),
                "difference_seconds": _seconds_text(
                    elapsed_seconds - last_data_seconds
                ),
            }
        )
    if any(value - active_seconds > Decimal("0.001") for value in candidates.values()):
        warnings.append(
            {
                "code": "active_seconds_conservative_minimum",
                "selected_seconds": _seconds_text(active_seconds),
                "candidates": {
                    name: _seconds_text(value) for name, value in candidates.items()
                },
            }
        )
    quality = (
        "PARTIAL"
        if any(
            warning["code"]
            in {
                "reported_duration_mismatch",
                "last_data_window_shorter_than_elapsed",
            }
            for warning in warnings
        )
        else "VALID"
    )
    return active_seconds, quality, warnings


def _canonical_row_hash(row: dict[str, str]) -> str:
    payload = json.dumps(
        row,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def validate_rows(
    fieldnames: list[str] | None,
    rows: Iterable[dict[str, str]],
) -> list[dict[str, Any]]:
    if fieldnames != EXPECTED_HEADERS:
        raise ValueError(f"unexpected execution CSV headers: {fieldnames!r}")

    parsed_rows: list[dict[str, Any]] = []
    seen_test_ids: set[str] = set()
    status_counts: Counter[str] = Counter()
    bench_counts: Counter[str] = Counter()
    case_counts: Counter[str] = Counter()

    for line_number, raw_row in enumerate(rows, start=2):
        if None in raw_row:
            raise ValueError(f"extra CSV columns at line {line_number}")
        row = dict(raw_row)
        test_id = row["test_id"].strip()
        case_code = row["test_case_id"].strip()
        case_name = row["test_case_name"].strip()
        bench_id = row["bench_id"].strip()
        source_status = row["status"].strip()

        if not test_id or test_id in seen_test_ids:
            raise ValueError(
                f"missing or duplicate test_id at CSV line {line_number}: {test_id!r}"
            )
        if SOURCE_KEY_PATTERN.fullmatch(test_id) is None:
            raise ValueError(
                f"invalid test_id format at CSV line {line_number}: {test_id!r}"
            )
        seen_test_ids.add(test_id)
        target_part = case_target_part(case_code)
        if case_code not in EXPECTED_CASE_COUNTS:
            raise ValueError(
                f"unexpected test case at CSV line {line_number}: {case_code!r}"
            )
        if not case_name:
            raise ValueError(f"test_case_name is required at CSV line {line_number}")
        if bench_id not in EXPECTED_BENCH_COUNTS:
            raise ValueError(
                f"unexpected bench at CSV line {line_number}: {bench_id!r}"
            )
        if source_status not in EXPECTED_STATUS_COUNTS:
            raise ValueError(
                f"unexpected status at CSV line {line_number}: {source_status!r}"
            )

        started_at = _parse_timestamp(
            row["started_at"], field="started_at", line_number=line_number
        )
        ended_at = _parse_timestamp(
            row["ended_at"],
            field="ended_at",
            line_number=line_number,
            required=False,
        )
        last_data_at = _parse_timestamp(
            row["last_data_at"], field="last_data_at", line_number=line_number
        )
        source_created_at = _parse_timestamp(
            row["created_at"], field="created_at", line_number=line_number
        )
        source_updated_at = _parse_timestamp(
            row["updated_at"], field="updated_at", line_number=line_number
        )
        assert started_at is not None
        assert last_data_at is not None
        assert source_created_at is not None
        assert source_updated_at is not None

        if test_id.split("_", 2)[0] != case_code:
            raise ValueError(
                f"test_id/case mismatch at CSV line {line_number}: {test_id!r}"
            )
        if test_id.split("_", 2)[1] != started_at.strftime("%y%m%d%H%M"):
            raise ValueError(
                f"test_id/start time mismatch at CSV line {line_number}: {test_id!r}"
            )
        if not test_id.endswith(f"_{bench_id}"):
            raise ValueError(
                f"test_id/bench mismatch at CSV line {line_number}: {test_id!r}"
            )
        if source_status in FINAL_STATUSES and ended_at is None:
            raise ValueError(
                f"ended_at is required for final status at CSV line {line_number}"
            )
        if source_status in ACTIVE_STATUSES and ended_at is not None:
            raise ValueError(
                f"ended_at must be empty for active status at CSV line {line_number}"
            )
        if ended_at is not None and ended_at < started_at:
            raise ValueError(f"ended_at precedes started_at at CSV line {line_number}")
        if last_data_at < started_at:
            raise ValueError(
                f"last_data_at precedes started_at at CSV line {line_number}"
            )
        if ended_at is not None and last_data_at > ended_at:
            raise ValueError(
                f"last_data_at exceeds ended_at at CSV line {line_number}"
            )
        if source_updated_at < source_created_at:
            raise ValueError(
                f"updated_at precedes created_at at CSV line {line_number}"
            )

        duration_hours = _parse_decimal(
            row["duration_hours"],
            field="duration_hours",
            line_number=line_number,
        )
        reported_seconds = duration_hours * Decimal("3600")
        try:
            exception_count = int(row["exception_count"].strip())
        except ValueError as exc:
            raise ValueError(
                f"invalid exception_count at CSV line {line_number}"
            ) from exc
        if exception_count < 0 or str(exception_count) != row["exception_count"].strip():
            raise ValueError(
                f"exception_count must be a non-negative integer at CSV line {line_number}"
            )
        archive_source = row["archive_status"].strip().lower()
        if archive_source not in ARCHIVE_MAPPING:
            raise ValueError(
                f"unexpected archive_status at CSV line {line_number}: "
                f"{archive_source!r}"
            )

        asset_code, warnings = normalize_asset(
            case_code, row["sample_id"].strip(), row["robot_id"].strip()
        )
        normalized_status = normalize_status(
            source_status, row["summary"], row["issues"]
        )
        if normalized_status["source_status_conflict"]:
            warnings.append(
                {
                    "code": "source_status_semantic_conflict",
                    "field": "status",
                    "source_value": source_status,
                    "normalized_status": normalized_status["execution_status"],
                    "normalized_outcome": normalized_status["outcome"],
                    "termination_kind": normalized_status["termination_kind"],
                }
            )

        active_seconds: Decimal | None = None
        runtime_quality: str | None = None
        if ended_at is not None:
            active_seconds, runtime_quality, runtime_warnings = (
                conservative_active_seconds(
                    reported_seconds, started_at, ended_at, last_data_at
                )
            )
            warnings.extend(runtime_warnings)

        clock_quality = runtime_quality or "VALID"
        data_quality = (
            "PARTIAL"
            if runtime_quality == "PARTIAL"
            or normalized_status["source_status_conflict"]
            else "VALID"
        )
        parsed_rows.append(
            {
                **row,
                "line_number": line_number,
                "test_id": test_id,
                "case_code": case_code,
                "case_name": case_name,
                "target_part": target_part,
                "asset_code": asset_code,
                "bench_id": bench_id,
                "source_status": source_status,
                "started_at_value": started_at,
                "ended_at_value": ended_at,
                "last_data_at_value": last_data_at,
                "source_created_at_value": source_created_at,
                "source_updated_at_value": source_updated_at,
                "reported_duration_seconds": reported_seconds,
                "exception_count_value": exception_count,
                "archive_status_value": ARCHIVE_MAPPING[archive_source],
                "execution_status": normalized_status["execution_status"],
                "outcome": normalized_status["outcome"],
                "termination_kind": normalized_status["termination_kind"],
                "active_seconds": active_seconds,
                "runtime_quality": runtime_quality,
                "clock_quality": clock_quality,
                "data_quality": data_quality,
                "normalization_warnings": warnings,
                "row_hash": _canonical_row_hash(row),
                "raw_payload": row,
            }
        )
        status_counts[source_status] += 1
        bench_counts[bench_id] += 1
        case_counts[case_code] += 1

    if len(parsed_rows) != EXPECTED_ROW_COUNT:
        raise ValueError(
            f"expected exactly {EXPECTED_ROW_COUNT} execution rows, "
            f"got {len(parsed_rows)}"
        )
    if status_counts != EXPECTED_STATUS_COUNTS:
        raise ValueError(f"unexpected execution status counts: {dict(status_counts)!r}")
    if bench_counts != EXPECTED_BENCH_COUNTS:
        raise ValueError(f"unexpected execution bench counts: {dict(bench_counts)!r}")
    if case_counts != EXPECTED_CASE_COUNTS:
        raise ValueError(f"unexpected execution case counts: {dict(case_counts)!r}")

    normalized_keys = [
        (
            row["case_code"],
            row["started_at_value"].strftime("%y%m%d%H%M"),
            row["asset_code"],
            row["bench_id"],
        )
        for row in parsed_rows
    ]
    duplicate_keys = [
        key for key, count in Counter(normalized_keys).items() if count > 1
    ]
    if duplicate_keys:
        raise ValueError(
            f"normalization would create duplicate execution codes: {duplicate_keys!r}"
        )
    return parsed_rows


def load_csv(csv_path: Path) -> list[dict[str, Any]]:
    with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        return validate_rows(reader.fieldnames, reader)


def _json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _scalar(connection: Any, statement: str, parameters: dict[str, Any]) -> Any:
    return connection.execute(text(statement), parameters).scalar_one()


def _prepare_import_context(
    connection: Any,
    rows: list[dict[str, Any]],
) -> dict[str, Any]:
    source_id = _scalar(
        connection,
        """
        INSERT INTO integration.ingestion_source(
            source_code, source_type, direction, status, config_reference
        ) VALUES (
            :source_code, 'LEGACY_IMPORT', 'INBOUND', 'ACTIVE', :source_file
        )
        ON CONFLICT (source_code) DO UPDATE SET
            source_type = 'LEGACY_IMPORT',
            direction = 'INBOUND',
            status = 'ACTIVE',
            config_reference = EXCLUDED.config_reference
        RETURNING id
        """,
        {"source_code": SOURCE_CODE, "source_file": SOURCE_FILE},
    )
    connection.execute(
        text(
            """
            INSERT INTO integration.import_batch(
                batch_code, source_system, status, started_at, completed_at,
                source_summary, result_summary
            ) VALUES (
                :batch_code, :source_code, 'RUNNING', clock_timestamp(), NULL,
                CAST(:source_summary AS jsonb), '{}'::jsonb
            )
            ON CONFLICT (batch_code) DO UPDATE SET
                source_system = EXCLUDED.source_system,
                status = 'RUNNING',
                started_at = clock_timestamp(),
                completed_at = NULL,
                source_summary = EXCLUDED.source_summary,
                result_summary = '{}'::jsonb
            """
        ),
        {
            "batch_code": IMPORT_BATCH_CODE,
            "source_code": SOURCE_CODE,
            "source_summary": _json(
                {
                    "sourceFile": SOURCE_FILE,
                    "expectedRows": EXPECTED_ROW_COUNT,
                    "rowCount": len(rows),
                }
            ),
        },
    )

    site_id = _scalar(
        connection,
        """
        INSERT INTO test.site(site_code, name, location, enabled)
        VALUES (
            'LEGACY-EXEC-20260918',
            'Legacy execution import site',
            'Source environments retained per execution result',
            true
        )
        ON CONFLICT (site_code) DO UPDATE SET
            name = EXCLUDED.name,
            location = EXCLUDED.location,
            enabled = true
        RETURNING id
        """,
        {},
    )
    lab_id = _scalar(
        connection,
        """
        INSERT INTO test.lab(site_id, lab_code, name, enabled)
        VALUES (
            :site_id, 'EXECUTION-HISTORY', 'Legacy execution history lab', true
        )
        ON CONFLICT (site_id, lab_code) DO UPDATE SET
            name = EXCLUDED.name,
            enabled = true
        RETURNING id
        """,
        {"site_id": site_id},
    )
    station_ids: dict[str, int] = {}
    for bench_id in sorted(EXPECTED_BENCH_COUNTS):
        result = connection.execute(
            text(
                """
                INSERT INTO test.station(
                    lab_id, station_code, name, station_type, enabled
                ) VALUES (
                    :lab_id, :station_code, :name, 'MODULE', true
                )
                ON CONFLICT (station_code) DO UPDATE SET
                    name = EXCLUDED.name,
                    station_type = 'MODULE',
                    enabled = true
                WHERE test.station.lab_id = EXCLUDED.lab_id
                RETURNING id
                """
            ),
            {
                "lab_id": lab_id,
                "station_code": bench_id,
                "name": f"Legacy reliability bench {bench_id}",
            },
        ).scalar_one_or_none()
        if result is None:
            raise ValueError(
                f"station {bench_id} already belongs to a non-legacy lab"
            )
        station_ids[bench_id] = result

    batch_id = _scalar(
        connection,
        """
        INSERT INTO test.manufacturing_batch(
            batch_code, asset_kind, product_family, notes
        ) VALUES (
            'LEGACY-EXEC-20260918-MODULES',
            'MODULE',
            'RP1.3-LEGACY',
            'Normalized assets from executions_202609181314.csv'
        )
        ON CONFLICT (batch_code) DO UPDATE SET
            notes = EXCLUDED.notes
        RETURNING id
        """,
        {},
    )
    program_id = _scalar(
        connection,
        """
        INSERT INTO test.test_program(
            program_code, name, objective, status
        ) VALUES (
            'LEGACY-EXECUTION-HISTORY-20260918',
            'Legacy execution history import',
            'Preserve audited execution history without formal MTBF inclusion',
            'ACTIVE'
        )
        ON CONFLICT (program_code) DO UPDATE SET
            name = EXCLUDED.name,
            objective = EXCLUDED.objective,
            status = 'ACTIVE'
        RETURNING id
        """,
        {},
    )

    rows_by_part: dict[str, list[dict[str, Any]]] = defaultdict(list)
    rows_by_asset: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        rows_by_part[row["target_part"]].append(row)
        rows_by_asset[row["asset_code"]].append(row)

    campaign_ids: dict[str, int] = {}
    for target_part, part_rows in sorted(rows_by_part.items()):
        has_active = any(row["source_status"] in ACTIVE_STATUSES for row in part_rows)
        campaign_ids[target_part] = _scalar(
            connection,
            """
            INSERT INTO test.test_campaign(
                program_id, site_id, campaign_code, name, asset_kind,
                status, planned_start, planned_end
            ) VALUES (
                :program_id, :site_id, :campaign_code, :name, 'MODULE',
                :status, :planned_start, :planned_end
            )
            ON CONFLICT (program_id, campaign_code) DO UPDATE SET
                site_id = EXCLUDED.site_id,
                name = EXCLUDED.name,
                status = EXCLUDED.status,
                planned_start = EXCLUDED.planned_start,
                planned_end = EXCLUDED.planned_end
            RETURNING id
            """,
            {
                "program_id": program_id,
                "site_id": site_id,
                "campaign_code": f"LEGACY-EXEC-{target_part}-20260918",
                "name": f"Legacy {target_part} execution history",
                "status": "ACTIVE" if has_active else "CLOSED",
                "planned_start": min(
                    row["started_at_value"] for row in part_rows
                ),
                "planned_end": max(
                    (
                        row["ended_at_value"]
                        for row in part_rows
                        if row["ended_at_value"] is not None
                    ),
                    default=None,
                ),
            },
        )

    asset_context: dict[str, dict[str, int]] = {}
    for asset_code, asset_rows in sorted(rows_by_asset.items()):
        target_part = asset_rows[0]["target_part"]
        sequence = int(asset_code.rsplit("-", 1)[1])
        identifiers = sorted(
            {
                identifier
                for row in asset_rows
                for identifier in (row["sample_id"].strip(), row["robot_id"].strip())
            }
        )
        asset_id_result = connection.execute(
            text(
                """
                INSERT INTO test.asset(
                    asset_code, asset_kind, product_family, batch_id,
                    serial_number, lifecycle_status, display_name, notes
                ) VALUES (
                    :asset_code, 'MODULE', 'RP1.3-LEGACY', :batch_id,
                    :serial_number, 'ACTIVE', :display_name,
                    'Normalized legacy execution-history asset'
                )
                ON CONFLICT (asset_code) DO UPDATE SET
                    lifecycle_status = 'ACTIVE',
                    display_name = EXCLUDED.display_name,
                    notes = EXCLUDED.notes
                WHERE test.asset.product_family = 'RP1.3-LEGACY'
                RETURNING id
                """
            ),
            {
                "asset_code": asset_code,
                "batch_id": batch_id,
                "serial_number": f"LEGACY-{target_part}-{sequence:03d}",
                "display_name": f"Legacy {target_part} module {sequence:03d}",
            },
        ).scalar_one_or_none()
        if asset_id_result is None:
            raise ValueError(
                f"normalized asset code {asset_code} is already used by non-legacy data"
            )
        asset_id = asset_id_result
        connection.execute(
            text(
                """
                INSERT INTO test.module_profile(
                    asset_id, module_type, part_number,
                    rated_specification, target_part_code
                ) VALUES (
                    :asset_id, :target_part, :part_number,
                    CAST(:rated_specification AS jsonb), :target_part
                )
                ON CONFLICT (asset_id) DO UPDATE SET
                    module_type = EXCLUDED.module_type,
                    part_number = EXCLUDED.part_number,
                    rated_specification = EXCLUDED.rated_specification,
                    target_part_code = EXCLUDED.target_part_code
                """
            ),
            {
                "asset_id": asset_id,
                "target_part": target_part,
                "part_number": f"LEGACY-{target_part}",
                "rated_specification": _json(
                    {
                        "importSource": SOURCE_CODE,
                        "sourceIdentifiers": identifiers,
                    }
                ),
            },
        )
        campaign_id = campaign_ids[target_part]
        joined_at = min(row["started_at_value"] for row in asset_rows)
        connection.execute(
            text(
                """
                INSERT INTO test.campaign_asset(
                    campaign_id, asset_id, participation_role, joined_at
                ) VALUES (
                    :campaign_id, :asset_id, 'PRIMARY', :joined_at
                )
                ON CONFLICT (campaign_id, asset_id) DO UPDATE SET
                    participation_role = 'PRIMARY',
                    joined_at = EXCLUDED.joined_at,
                    left_at = NULL
                """
            ),
            {
                "campaign_id": campaign_id,
                "asset_id": asset_id,
                "joined_at": joined_at,
            },
        )
        config_id = _scalar(
            connection,
            """
            INSERT INTO test.configuration_snapshot(
                asset_id, fingerprint, hardware_manifest, software_manifest,
                parameter_manifest, reliability_impact, captured_from,
                effective_from
            ) VALUES (
                :asset_id, :fingerprint, CAST(:hardware_manifest AS jsonb),
                CAST(:software_manifest AS jsonb), '{}'::jsonb,
                'UNKNOWN', :source_code, :effective_from
            )
            ON CONFLICT (asset_id, fingerprint) DO UPDATE SET
                hardware_manifest = EXCLUDED.hardware_manifest,
                captured_from = EXCLUDED.captured_from,
                effective_from = EXCLUDED.effective_from
            RETURNING id
            """,
            {
                "asset_id": asset_id,
                "fingerprint": f"{SOURCE_CODE}:{asset_code}",
                "hardware_manifest": _json({"sourceIdentifiers": identifiers}),
                "software_manifest": _json({"legacyImport": True}),
                "source_code": SOURCE_CODE,
                "effective_from": joined_at,
            },
        )
        has_active = any(row["source_status"] in ACTIVE_STATUSES for row in asset_rows)
        ended_at = (
            None
            if has_active
            else max(
                row["ended_at_value"]
                for row in asset_rows
                if row["ended_at_value"] is not None
            )
        )
        cycle_id = _scalar(
            connection,
            """
            INSERT INTO test.test_cycle(
                cycle_code, campaign_id, asset_id, status,
                baseline_confirmed_at, started_at, ended_at, close_reason
            ) VALUES (
                :cycle_code, :campaign_id, :asset_id, :status,
                NULL, :started_at, :ended_at, :close_reason
            )
            ON CONFLICT (cycle_code) DO UPDATE SET
                campaign_id = EXCLUDED.campaign_id,
                asset_id = EXCLUDED.asset_id,
                status = EXCLUDED.status,
                started_at = EXCLUDED.started_at,
                ended_at = EXCLUDED.ended_at,
                close_reason = EXCLUDED.close_reason
            RETURNING id
            """,
            {
                "cycle_code": f"LEGACY-EXEC-{target_part}-{sequence:03d}-20260918",
                "campaign_id": campaign_id,
                "asset_id": asset_id,
                "status": "ACTIVE" if has_active else "CLOSED",
                "started_at": joined_at,
                "ended_at": ended_at,
                "close_reason": "" if has_active else "Legacy history import boundary",
            },
        )
        _scalar(
            connection,
            """
            INSERT INTO test.analysis_segment(
                campaign_id, cycle_id, configuration_snapshot_id,
                segment_no, reason, started_at, ended_at
            ) VALUES (
                :campaign_id, :cycle_id, :config_id,
                1, 'ORIGINAL', :started_at, :ended_at
            )
            ON CONFLICT (cycle_id, segment_no) DO UPDATE SET
                campaign_id = EXCLUDED.campaign_id,
                configuration_snapshot_id = EXCLUDED.configuration_snapshot_id,
                reason = 'ORIGINAL',
                started_at = EXCLUDED.started_at,
                ended_at = EXCLUDED.ended_at
            RETURNING id
            """,
            {
                "campaign_id": campaign_id,
                "cycle_id": cycle_id,
                "config_id": config_id,
                "started_at": joined_at,
                "ended_at": ended_at,
            },
        )
        asset_context[asset_code] = {
            "asset_id": asset_id,
            "campaign_id": campaign_id,
            "configuration_snapshot_id": config_id,
            "cycle_id": cycle_id,
        }

    return {
        "source_id": source_id,
        "station_ids": station_ids,
        "asset_context": asset_context,
        "campaign_ids": campaign_ids,
    }


def _prepare_case_versions(
    connection: Any,
    rows: list[dict[str, Any]],
) -> dict[str, int]:
    case_names = {row["case_code"]: row["case_name"] for row in rows}
    performance_case = "PERF-SARM-001"
    connection.execute(
        text(
            """
            INSERT INTO catalog.test_case(
                case_code, name, asset_kind, target_part_code,
                domain, evidence_type, enabled
            ) VALUES (
                :case_code, :name, 'MODULE', 'SARM',
                'PERFORMANCE', 'RUNTIME_AND_METRIC', true
            )
            ON CONFLICT (case_code) DO NOTHING
            """
        ),
        {"case_code": performance_case, "name": case_names[performance_case]},
    )
    performance_metadata = connection.execute(
        text(
            """
            SELECT id, asset_kind, target_part_code, domain
            FROM catalog.test_case
            WHERE case_code = :case_code
            """
        ),
        {"case_code": performance_case},
    ).one()
    if tuple(performance_metadata[1:]) != ("MODULE", "SARM", "PERFORMANCE"):
        raise ValueError(
            "existing PERF-SARM-001 must be MODULE/SARM/PERFORMANCE"
        )
    published_performance_version = connection.execute(
        text(
            """
            SELECT id
            FROM catalog.test_case_version
            WHERE test_case_id = :test_case_id AND status = 'PUBLISHED'
            ORDER BY published_at DESC NULLS LAST, created_at DESC, id DESC
            LIMIT 1
            """
        ),
        {"test_case_id": performance_metadata[0]},
    ).scalar_one_or_none()
    if published_performance_version is None:
        connection.execute(
            text(
                """
                INSERT INTO catalog.test_case_version(
                    test_case_id, version, procedure_spec, stage_definitions,
                    equipment_requirements, metric_requirements,
                    sampling_requirements, fault_rules, termination_rules,
                    decision_rules, source_references, deviation_notes,
                    status, published_at
                ) VALUES (
                    :test_case_id, 'LEGACY-IMPORT-20260918',
                    CAST(:procedure_spec AS jsonb),
                    '[]'::jsonb, '{}'::jsonb, '[]'::jsonb, '{}'::jsonb,
                    '{}'::jsonb, '{}'::jsonb, '{}'::jsonb,
                    CAST(:source_references AS jsonb),
                    'Imported only because the source case was absent.',
                    'PUBLISHED', clock_timestamp()
                )
                ON CONFLICT (test_case_id, version) DO NOTHING
                """
            ),
            {
                "test_case_id": performance_metadata[0],
                "procedure_spec": _json(
                    {"importOnly": True, "source": "execution CSV"}
                ),
                "source_references": _json(
                    [{"source": SOURCE_CODE, "file": SOURCE_FILE}]
                ),
            },
        )

    case_versions: dict[str, int] = {}
    for case_code in sorted(case_names):
        metadata = connection.execute(
            text(
                """
                SELECT id, asset_kind, target_part_code
                FROM catalog.test_case
                WHERE case_code = :case_code
                """
            ),
            {"case_code": case_code},
        ).one_or_none()
        if metadata is None:
            raise ValueError(f"required test case is missing: {case_code}")
        if tuple(metadata[1:]) != ("MODULE", case_target_part(case_code)):
            raise ValueError(
                f"test case {case_code} has an incompatible target part"
            )
        version_id = connection.execute(
            text(
                """
                SELECT id
                FROM catalog.test_case_version
                WHERE test_case_id = :test_case_id AND status = 'PUBLISHED'
                ORDER BY published_at DESC NULLS LAST, created_at DESC, id DESC
                LIMIT 1
                """
            ),
            {"test_case_id": metadata[0]},
        ).scalar_one_or_none()
        if version_id is None:
            raise ValueError(f"test case has no published version: {case_code}")
        case_versions[case_code] = version_id
    return case_versions


def _upsert_execution_row(
    connection: Any,
    row: dict[str, Any],
    *,
    source_id: int,
    station_id: int,
    context: dict[str, int],
    case_version_id: int,
) -> dict[str, int]:
    execution_id = _scalar(
        connection,
        """
        INSERT INTO test.test_execution(
            execution_code, campaign_id, cycle_id, asset_id,
            configuration_snapshot_id, test_case_version_id, station_id,
            source_id, source_execution_key, status, source_time, received_at,
            normalized_started_at, normalized_ended_at,
            clock_quality, data_quality, created_at, updated_at, code_timestamp
        ) VALUES (
            :test_id, :campaign_id, :cycle_id, :asset_id,
            :config_id, :case_version_id, :station_id,
            :source_id, :test_id, :execution_status, :source_time, :received_at,
            :started_at, :ended_at, :clock_quality, :data_quality,
            :created_at, :updated_at, :started_at
        )
        ON CONFLICT (source_id, source_execution_key)
        WHERE source_id IS NOT NULL AND source_execution_key IS NOT NULL
        DO UPDATE SET
            campaign_id = EXCLUDED.campaign_id,
            cycle_id = EXCLUDED.cycle_id,
            asset_id = EXCLUDED.asset_id,
            configuration_snapshot_id = EXCLUDED.configuration_snapshot_id,
            test_case_version_id = EXCLUDED.test_case_version_id,
            station_id = EXCLUDED.station_id,
            execution_code = EXCLUDED.execution_code,
            status = EXCLUDED.status,
            source_time = EXCLUDED.source_time,
            received_at = EXCLUDED.received_at,
            normalized_started_at = EXCLUDED.normalized_started_at,
            normalized_ended_at = EXCLUDED.normalized_ended_at,
            clock_quality = EXCLUDED.clock_quality,
            data_quality = EXCLUDED.data_quality,
            created_at = EXCLUDED.created_at,
            code_timestamp = EXCLUDED.code_timestamp
        RETURNING id
        """,
        {
            "test_id": row["test_id"],
            "campaign_id": context["campaign_id"],
            "cycle_id": context["cycle_id"],
            "asset_id": context["asset_id"],
            "config_id": context["configuration_snapshot_id"],
            "case_version_id": case_version_id,
            "station_id": station_id,
            "source_id": source_id,
            "execution_status": row["execution_status"],
            "source_time": row["started_at_value"],
            "received_at": row["source_updated_at_value"],
            "started_at": row["started_at_value"],
            "ended_at": row["ended_at_value"],
            "clock_quality": row["clock_quality"],
            "data_quality": row["data_quality"],
            "created_at": row["source_created_at_value"],
            "updated_at": row["source_updated_at_value"],
        },
    )
    warnings_json = _json(row["normalization_warnings"])
    connection.execute(
        text(
            """
            INSERT INTO test.execution_result(
                execution_id, source_status, outcome, termination_kind,
                summary, issues, exception_count, reported_duration_seconds,
                executor_display, environment_label, last_data_at,
                telemetry_source, archive_status, archive_path,
                report_reference, normalization_flags,
                source_created_at, source_updated_at, created_at, updated_at
            ) VALUES (
                :execution_id, :source_status, :outcome, :termination_kind,
                :summary, :issues, :exception_count, :reported_duration_seconds,
                :executor, :environment, :last_data_at,
                :telemetry_source, :archive_status, :archive_path,
                :report_reference, CAST(:warnings AS jsonb),
                :source_created_at, :source_updated_at,
                :source_created_at, :source_updated_at
            )
            ON CONFLICT (execution_id) DO UPDATE SET
                source_status = EXCLUDED.source_status,
                outcome = EXCLUDED.outcome,
                termination_kind = EXCLUDED.termination_kind,
                summary = EXCLUDED.summary,
                issues = EXCLUDED.issues,
                exception_count = EXCLUDED.exception_count,
                reported_duration_seconds = EXCLUDED.reported_duration_seconds,
                executor_display = EXCLUDED.executor_display,
                environment_label = EXCLUDED.environment_label,
                last_data_at = EXCLUDED.last_data_at,
                telemetry_source = EXCLUDED.telemetry_source,
                archive_status = EXCLUDED.archive_status,
                archive_path = EXCLUDED.archive_path,
                report_reference = EXCLUDED.report_reference,
                normalization_flags = EXCLUDED.normalization_flags,
                source_created_at = EXCLUDED.source_created_at,
                source_updated_at = EXCLUDED.source_updated_at
            """
        ),
        {
            "execution_id": execution_id,
            "source_status": row["source_status"],
            "outcome": row["outcome"],
            "termination_kind": row["termination_kind"],
            "summary": row["summary"],
            "issues": row["issues"],
            "exception_count": row["exception_count_value"],
            "reported_duration_seconds": row["reported_duration_seconds"],
            "executor": row["executor"],
            "environment": row["environment"],
            "last_data_at": row["last_data_at_value"],
            "telemetry_source": row["telemetry_source"],
            "archive_status": row["archive_status_value"],
            "archive_path": row["archive_path"],
            "report_reference": row["report_url"],
            "warnings": warnings_json,
            "source_created_at": row["source_created_at_value"],
            "source_updated_at": row["source_updated_at_value"],
        },
    )
    connection.execute(
        text(
            """
            INSERT INTO integration.execution_import_record(
                source_id, source_execution_key, execution_id,
                source_file, source_row_number, row_hash,
                raw_payload, normalization_warnings, imported_at
            ) VALUES (
                :source_id, :test_id, :execution_id,
                :source_file, :line_number, :row_hash,
                CAST(:raw_payload AS jsonb), CAST(:warnings AS jsonb),
                clock_timestamp()
            )
            ON CONFLICT (source_id, source_execution_key) DO UPDATE SET
                execution_id = EXCLUDED.execution_id,
                source_file = EXCLUDED.source_file,
                source_row_number = EXCLUDED.source_row_number,
                row_hash = EXCLUDED.row_hash,
                raw_payload = EXCLUDED.raw_payload,
                normalization_warnings = EXCLUDED.normalization_warnings,
                imported_at = clock_timestamp()
            """
        ),
        {
            "source_id": source_id,
            "test_id": row["test_id"],
            "execution_id": execution_id,
            "source_file": SOURCE_FILE,
            "line_number": row["line_number"],
            "row_hash": row["row_hash"],
            "raw_payload": _json(row["raw_payload"]),
            "warnings": warnings_json,
        },
    )

    runtime_count = 0
    if row["ended_at_value"] is not None:
        connection.execute(
            text(
                """
                INSERT INTO test.runtime_interval(
                    campaign_id, asset_id, cycle_id, execution_id,
                    source_id, source_kind, source_record_key,
                    source_time, received_at, started_at, ended_at,
                    active_seconds, clock_quality, data_quality, source_method
                ) VALUES (
                    :campaign_id, :asset_id, :cycle_id, :execution_id,
                    :source_id, 'LEGACY_REPORTED', :source_record_key,
                    :source_time, :received_at, :started_at, :ended_at,
                    :active_seconds, :quality, :quality, :source_method
                )
                ON CONFLICT (source_id, source_record_key)
                WHERE source_id IS NOT NULL AND source_record_key IS NOT NULL
                DO UPDATE SET
                    campaign_id = EXCLUDED.campaign_id,
                    asset_id = EXCLUDED.asset_id,
                    cycle_id = EXCLUDED.cycle_id,
                    execution_id = EXCLUDED.execution_id,
                    source_time = EXCLUDED.source_time,
                    received_at = EXCLUDED.received_at,
                    started_at = EXCLUDED.started_at,
                    ended_at = EXCLUDED.ended_at,
                    active_seconds = EXCLUDED.active_seconds,
                    clock_quality = EXCLUDED.clock_quality,
                    data_quality = EXCLUDED.data_quality,
                    source_method = EXCLUDED.source_method,
                    voided = false,
                    void_reason = '',
                    voided_by = NULL,
                    voided_at = NULL
                """
            ),
            {
                "campaign_id": context["campaign_id"],
                "asset_id": context["asset_id"],
                "cycle_id": context["cycle_id"],
                "execution_id": execution_id,
                "source_id": source_id,
                "source_record_key": f"{row['test_id']}:runtime",
                "source_time": row["ended_at_value"],
                "received_at": row["source_updated_at_value"],
                "started_at": row["started_at_value"],
                "ended_at": row["ended_at_value"],
                "active_seconds": row["active_seconds"],
                "quality": row["runtime_quality"],
                "source_method": (
                    "execution_csv_conservative_min("
                    "reported,elapsed,last_data_window)"
                ),
            },
        )
        runtime_count = 1

    abnormal_termination = row["termination_kind"] not in {
        "NORMAL",
        "SCHEDULED",
        "RUNNING",
    }
    event_count = 0
    if (
        row["issues"].strip()
        or row["exception_count_value"] > 0
        or abnormal_termination
        or row["execution_status"] in {"FAILED", "BLOCKED"}
    ):
        event_time = (
            row["ended_at_value"]
            or row["last_data_at_value"]
            or row["started_at_value"]
        )
        connection.execute(
            text(
                """
                INSERT INTO test.test_event(
                    campaign_id, asset_id, cycle_id, execution_id,
                    event_type, source_time, received_at, normalized_time,
                    clock_quality, data_quality, source_id, source_event_key,
                    payload
                ) VALUES (
                    :campaign_id, :asset_id, :cycle_id, :execution_id,
                    'LEGACY_EXECUTION_ISSUE', :event_time, :received_at, :event_time,
                    :clock_quality, :data_quality, :source_id, :source_event_key,
                    CAST(:payload AS jsonb)
                )
                ON CONFLICT (source_id, source_event_key)
                WHERE source_id IS NOT NULL AND source_event_key IS NOT NULL
                DO UPDATE SET
                    campaign_id = EXCLUDED.campaign_id,
                    asset_id = EXCLUDED.asset_id,
                    cycle_id = EXCLUDED.cycle_id,
                    execution_id = EXCLUDED.execution_id,
                    source_time = EXCLUDED.source_time,
                    received_at = EXCLUDED.received_at,
                    normalized_time = EXCLUDED.normalized_time,
                    clock_quality = EXCLUDED.clock_quality,
                    data_quality = EXCLUDED.data_quality,
                    payload = EXCLUDED.payload,
                    voided = false,
                    void_reason = '',
                    voided_by = NULL,
                    voided_at = NULL
                """
            ),
            {
                "campaign_id": context["campaign_id"],
                "asset_id": context["asset_id"],
                "cycle_id": context["cycle_id"],
                "execution_id": execution_id,
                "event_time": event_time,
                "received_at": row["source_updated_at_value"],
                "clock_quality": row["clock_quality"],
                "data_quality": row["data_quality"],
                "source_id": source_id,
                "source_event_key": f"{row['test_id']}:issue-or-termination",
                "payload": _json(
                    {
                        "sourceStatus": row["source_status"],
                        "normalizedStatus": row["execution_status"],
                        "outcome": row["outcome"],
                        "terminationKind": row["termination_kind"],
                        "summary": row["summary"],
                        "issues": row["issues"],
                        "exceptionCount": row["exception_count_value"],
                    }
                ),
            },
        )
        event_count = 1

    artifact_count = 0
    report_reference = row["report_url"].strip()
    if report_reference:
        availability = (
            "ARCHIVED"
            if row["archive_status_value"] in {"IMPORTED", "ARCHIVED"}
            else "UNAVAILABLE_LEGACY"
        )
        file_name = report_reference.rsplit("/", 1)[-1]
        connection.execute(
            text(
                """
                INSERT INTO integration.artifact(
                    campaign_id, asset_id, cycle_id, execution_id, kind,
                    availability_status, object_key, source_location,
                    file_name, mime_type
                ) VALUES (
                    :campaign_id, :asset_id, :cycle_id, :execution_id,
                    'LEGACY_REPORT_REFERENCE', :availability,
                    NULL, :source_location, :file_name,
                    'application/octet-stream'
                )
                ON CONFLICT (execution_id, kind)
                WHERE execution_id IS NOT NULL
                  AND kind = 'LEGACY_REPORT_REFERENCE'
                DO UPDATE SET
                    campaign_id = EXCLUDED.campaign_id,
                    asset_id = EXCLUDED.asset_id,
                    cycle_id = EXCLUDED.cycle_id,
                    availability_status = EXCLUDED.availability_status,
                    object_key = NULL,
                    source_location = EXCLUDED.source_location,
                    file_name = EXCLUDED.file_name,
                    mime_type = EXCLUDED.mime_type,
                    size_bytes = NULL,
                    sha256 = NULL
                """
            ),
            {
                "campaign_id": context["campaign_id"],
                "asset_id": context["asset_id"],
                "cycle_id": context["cycle_id"],
                "execution_id": execution_id,
                "availability": availability,
                "source_location": report_reference,
                "file_name": file_name,
            },
        )
        artifact_count = 1
    return {
        "execution": 1,
        "result": 1,
        "lineage": 1,
        "runtime": runtime_count,
        "event": event_count,
        "artifact": artifact_count,
    }


def import_execution_history(
    connection: Any,
    csv_path: Path,
) -> dict[str, Any]:
    rows = load_csv(csv_path)
    context = _prepare_import_context(connection, rows)
    case_versions = _prepare_case_versions(connection, rows)
    counts: Counter[str] = Counter()
    for row in rows:
        row_counts = _upsert_execution_row(
            connection,
            row,
            source_id=context["source_id"],
            station_id=context["station_ids"][row["bench_id"]],
            context=context["asset_context"][row["asset_code"]],
            case_version_id=case_versions[row["case_code"]],
        )
        counts.update(row_counts)

    result_summary = {
        "sourceCode": SOURCE_CODE,
        "sourceFile": SOURCE_FILE,
        "rows": len(rows),
        "assets": len(context["asset_context"]),
        "campaigns": len(context["campaign_ids"]),
        "stations": len(context["station_ids"]),
        "executions": counts["execution"],
        "results": counts["result"],
        "lineage": counts["lineage"],
        "runtimeIntervals": counts["runtime"],
        "events": counts["event"],
        "artifacts": counts["artifact"],
    }
    connection.execute(
        text(
            """
            UPDATE integration.import_batch
            SET status = 'SUCCEEDED',
                completed_at = clock_timestamp(),
                result_summary = CAST(:result_summary AS jsonb)
            WHERE batch_code = :batch_code
            """
        ),
        {
            "batch_code": IMPORT_BATCH_CODE,
            "result_summary": _json(result_summary),
        },
    )
    return result_summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Idempotently import normalized legacy execution history."
    )
    parser.add_argument(
        "csv_path",
        nargs="?",
        type=Path,
        default=Path(__file__).resolve().parent / SOURCE_FILE,
    )
    args = parser.parse_args(argv)
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL is required")
    engine = create_engine(database_url)
    with engine.begin() as connection:
        summary = import_execution_history(connection, args.csv_path)
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
