from __future__ import annotations

import hashlib
import json
import logging
import time
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.db import SessionLocal
from app.domain.mtbf import (
    DEFAULT_CONFIDENCE_LEVELS,
    IMPLEMENTATION_KEY,
    IMPLEMENTATION_VERSION,
    calculate_mtbf,
)


logger = logging.getLogger("rp1.worker")


def interval_union_seconds(intervals: list[tuple[datetime, datetime]]) -> Decimal:
    """Return the exact union duration for half-open [start, end) intervals."""
    if not intervals:
        return Decimal("0")
    ordered = sorted(intervals, key=lambda item: (item[0], item[1]))
    merged: list[list[datetime]] = []
    for start, end in ordered:
        if end <= start:
            continue
        if not merged or start > merged[-1][1]:
            merged.append([start, end])
        elif end > merged[-1][1]:
            merged[-1][1] = end
    return sum(
        (Decimal(str((end - start).total_seconds())) for start, end in merged),
        Decimal("0"),
    )


def _set_worker_context(session: Session, request_id: str) -> None:
    settings = get_settings()
    session.execute(
        text("SELECT iam.set_request_context(:user_id, :request_id, :reason)"),
        {
            "user_id": settings.worker_user_public_id,
            "request_id": request_id,
            "reason": "campaign MTBF deterministic recomputation",
        },
    )


def claim_job(session: Session, worker_id: str) -> dict[str, Any] | None:
    row = session.execute(
        text("SELECT * FROM reliability.claim_recompute_job(:worker_id)"),
        {"worker_id": worker_id},
    ).mappings().first()
    return dict(row) if row else None


def _interval_rows(
    session: Session,
    campaign_id: int,
    scope_id: int,
    cutoff: datetime,
    as_of: datetime,
) -> list[dict[str, Any]]:
    rows = session.execute(
        text(
            """
            SELECT ri.public_id AS interval_public_id,
                   a.id AS asset_id, a.public_id AS asset_public_id,
                   a.asset_code, a.display_name,
                   ri.source_kind, ri.started_at,
                   CASE WHEN ri.ended_at IS NULL THEN NULL
                        ELSE LEAST(ri.ended_at, :cutoff) END AS ended_at,
                   ri.active_seconds,
                   CASE
                       WHEN ri.source_kind = 'LEGACY_REPORTED' THEN 'LEGACY_REPORTED'
                       WHEN ea.id IS NULL OR esa.id IS NULL THEN 'PENDING'
                       WHEN ea.assignment_status = 'PENDING_CONFIG_REVIEW' THEN 'PENDING'
                       WHEN esa.inclusion_status = 'PENDING' THEN 'PENDING'
                       WHEN ea.assignment_status = 'INVALID'
                         OR ea.quality_status IN ('INVALID', 'MISSING') THEN 'EXCLUDED'
                       WHEN esa.inclusion_status = 'INCLUDED'
                         AND ea.eligible
                         AND ea.assignment_status = 'CONFIRMED'
                         AND ea.quality_status IN ('VALID', 'PARTIAL') THEN 'INCLUDED'
                       ELSE 'EXCLUDED'
                   END AS disposition,
                   coalesce(nullif(esa.rationale, ''), nullif(ea.exclusion_reason, ''),
                            CASE WHEN ea.id IS NULL THEN '缺少暴露资格评估'
                                 WHEN esa.id IS NULL THEN '缺少scope归属'
                                 ELSE ri.source_method END) AS reason
            FROM test.runtime_interval ri
            JOIN test.asset a ON a.id = ri.asset_id
            LEFT JOIN reliability.exposure_assessment ea
              ON ea.runtime_interval_id = ri.id
            LEFT JOIN reliability.exposure_scope_assignment esa
              ON esa.assessment_id = ea.id AND esa.scope_id = :scope_id
            WHERE ri.campaign_id = :campaign_id
              AND NOT ri.voided
              AND ri.created_at <= :as_of
              AND (
                  ri.source_kind = 'LEGACY_REPORTED'
                  OR (ri.started_at < :cutoff AND ri.ended_at IS NOT NULL)
              )
            ORDER BY a.id, ri.started_at NULLS FIRST, ri.id
            """
        ),
        {
            "campaign_id": campaign_id,
            "scope_id": scope_id,
            "cutoff": cutoff,
            "as_of": as_of,
        },
    ).mappings()
    return [dict(row) for row in rows]


def _failure_rows(
    session: Session,
    campaign_id: int,
    scope_id: int,
    cutoff: datetime,
    as_of: datetime,
) -> list[dict[str, Any]]:
    rows = session.execute(
        text(
            """
            SELECT i.public_id, i.failure_group_key, i.classification,
                   i.review_status, i.started_at,
                   coalesce(isa.status, 'PENDING') AS scope_status,
                   coalesce((
                       SELECT array_agg(ec.unified_code ORDER BY ec.unified_code)
                       FROM reliability.interruption_error_code iec
                       JOIN catalog.error_code ec ON ec.id = iec.error_code_id
                       WHERE iec.interruption_id = i.id
                   ), ARRAY[]::text[]) AS error_codes
            FROM reliability.interruption i
            LEFT JOIN reliability.interruption_scope_assignment isa
              ON isa.interruption_id = i.id AND isa.scope_id = :scope_id
            WHERE i.campaign_id = :campaign_id
              AND NOT i.voided
              AND i.started_at <= :cutoff
              AND i.created_at <= :as_of
            ORDER BY i.started_at, i.id
            """
        ),
        {
            "campaign_id": campaign_id,
            "scope_id": scope_id,
            "cutoff": cutoff,
            "as_of": as_of,
        },
    ).mappings()
    return [dict(row) for row in rows]


def process_job(session: Session, job: dict[str, Any]) -> None:
    campaign_id = job.get("campaign_id")
    scope_id = job.get("scope_id")
    if not campaign_id or not scope_id:
        raise ValueError("recompute job is missing campaign_id or scope_id")

    context = session.execute(
        text(
            """
            SELECT cfg.campaign_id, cfg.scope_id, cfg.method_id,
                   cfg.target_seconds, cfg.confidence_levels,
                   c.public_id AS campaign_public_id, c.campaign_code,
                   c.name AS campaign_name, c.status AS campaign_status,
                   s.scope_code, s.name AS scope_name
            FROM reliability.campaign_mtbf_config cfg
            JOIN test.test_campaign c ON c.id = cfg.campaign_id
            JOIN reliability.mtbf_scope s ON s.id = cfg.scope_id
            JOIN reliability.statistics_method m ON m.id = cfg.method_id
            WHERE cfg.campaign_id = :campaign_id
              AND cfg.scope_id = :scope_id
              AND cfg.enabled
              AND m.implementation_key = :implementation_key
              AND m.status = 'PUBLISHED'
            """
        ),
        {
            "campaign_id": campaign_id,
            "scope_id": scope_id,
            "implementation_key": IMPLEMENTATION_KEY,
        },
    ).mappings().one()

    cutoff = datetime.now(timezone.utc)
    as_of = cutoff
    interval_rows = _interval_rows(session, campaign_id, scope_id, cutoff, as_of)

    status_intervals: dict[int, dict[str, list[tuple[datetime, datetime]]]] = defaultdict(
        lambda: defaultdict(list)
    )
    runtime_intervals: dict[int, list[tuple[datetime, datetime]]] = defaultdict(list)
    asset_meta: dict[int, dict[str, Any]] = {}
    legacy_seconds: dict[int, Decimal] = defaultdict(lambda: Decimal("0"))
    interval_details: list[dict[str, Any]] = []

    for row in interval_rows:
        asset_id = row["asset_id"]
        asset_meta[asset_id] = {
            "asset_id": str(row["asset_public_id"]),
            "asset_code": row["asset_code"],
            "display_name": row["display_name"],
        }
        if row["source_kind"] == "LEGACY_REPORTED":
            seconds = Decimal(row["active_seconds"])
            legacy_seconds[asset_id] += seconds
        else:
            if row["ended_at"] <= row["started_at"]:
                continue
            interval = (row["started_at"], row["ended_at"])
            runtime_intervals[asset_id].append(interval)
            status_intervals[asset_id][row["disposition"]].append(interval)
            seconds = Decimal(str((row["ended_at"] - row["started_at"]).total_seconds()))
        interval_details.append(
            {
                "interval_id": str(row["interval_public_id"]),
                "asset_id": str(row["asset_public_id"]),
                "asset_code": row["asset_code"],
                "status": row["disposition"],
                "started_at": row["started_at"].isoformat() if row["started_at"] else None,
                "ended_at": row["ended_at"].isoformat() if row["ended_at"] else None,
                "hours": float(seconds / Decimal("3600")),
                "reason": row["reason"] or "",
            }
        )

    included_total = Decimal("0")
    excluded_total = Decimal("0")
    pending_total = Decimal("0")
    runtime_total = Decimal("0")
    per_asset: list[dict[str, Any]] = []
    all_asset_ids = sorted(set(asset_meta) | set(runtime_intervals) | set(legacy_seconds))
    for asset_id in all_asset_ids:
        included = interval_union_seconds(status_intervals[asset_id].get("INCLUDED", []))
        excluded = interval_union_seconds(status_intervals[asset_id].get("EXCLUDED", []))
        pending = interval_union_seconds(status_intervals[asset_id].get("PENDING", []))
        runtime = interval_union_seconds(runtime_intervals[asset_id])
        included_total += included
        excluded_total += excluded
        pending_total += pending
        runtime_total += runtime
        per_asset.append(
            {
                **asset_meta[asset_id],
                "included_hours": float(included / Decimal("3600")),
                "excluded_hours": float(excluded / Decimal("3600")),
                "pending_hours": float(pending / Decimal("3600")),
                "runtime_hours": float(runtime / Decimal("3600")),
            }
        )

    failure_rows = _failure_rows(session, campaign_id, scope_id, cutoff, as_of)
    failure_groups: dict[str, dict[str, Any]] = {}
    for row in failure_rows:
        key = (row["failure_group_key"] or str(row["public_id"])).strip()
        group = failure_groups.setdefault(
            key,
            {
                "group_key": key,
                "counted": False,
                "pending": False,
                "interruption_ids": [],
                "error_codes": set(),
            },
        )
        group["counted"] = group["counted"] or (
            row["classification"] == "FAILED"
            and row["review_status"] in {"CONFIRMED", "RECLASSIFIED"}
            and row["scope_status"] == "INCLUDED"
        )
        group["pending"] = group["pending"] or (
            row["classification"] == "BLOCKED"
            or row["review_status"] == "PENDING"
            or row["scope_status"] == "PENDING"
        )
        group["interruption_ids"].append(str(row["public_id"]))
        group["error_codes"].update(row["error_codes"])

    relevant_failures = sum(1 for group in failure_groups.values() if group["counted"])
    pending_failures = sum(1 for group in failure_groups.values() if group["pending"])
    exposure_hours = float(included_total / Decimal("3600"))
    configured_levels = tuple(float(value) for value in context["confidence_levels"])
    levels = tuple(sorted(set(configured_levels + DEFAULT_CONFIDENCE_LEVELS)))
    result = calculate_mtbf(exposure_hours, relevant_failures, levels)

    total_test_seconds = runtime_total + sum(legacy_seconds.values(), Decimal("0"))
    confidence_bounds = {str(level): result.lower_bound(level) for level in levels}
    failure_evidence = [
        {
            **{key: value for key, value in group.items() if key != "error_codes"},
            "error_codes": sorted(group["error_codes"]),
        }
        for group in failure_groups.values()
    ]
    input_summary = {
        "campaign_id": str(context["campaign_public_id"]),
        "scope": context["scope_code"],
        "data_cutoff_at": cutoff.isoformat(),
        "knowledge_as_of_at": as_of.isoformat(),
        "interval_ids": [item["interval_id"] for item in interval_details],
        "failure_groups": failure_evidence,
    }
    output_summary = {
        "method": IMPLEMENTATION_KEY,
        "total_test_duration_hours": float(total_test_seconds / Decimal("3600")),
        "runtime_hours": float(runtime_total / Decimal("3600")),
        "eligible_exposure_hours": exposure_hours,
        "relevant_failure_count": relevant_failures,
        "pending_failure_count": pending_failures,
        "mtbf_point_estimate_hours": result.point_estimate_hours,
        "point_estimate_status": result.point_estimate_status,
        "confidence_bounds": confidence_bounds,
        "excluded_hours": float(excluded_total / Decimal("3600")),
        "pending_hours": float(pending_total / Decimal("3600")),
        "per_asset_exposure": per_asset,
        "time_breakdown": {"intervals": interval_details},
    }
    canonical_json = json.dumps(input_summary, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    input_hash = hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()
    actor_id = session.execute(text("SELECT iam.current_user_id()" )).scalar_one()

    run = session.execute(
        text(
            """
            INSERT INTO reliability.calculation_run(
                population_id, campaign_id, scope_id, method_id, status,
                data_cutoff_at, knowledge_as_of_at, input_hash,
                input_summary, method_parameters, output_summary,
                evidence_summary, implementation_version, initiated_by,
                started_at, completed_at
            ) VALUES (
                NULL, :campaign_id, :scope_id, :method_id, 'SUCCEEDED',
                :cutoff, :as_of, :input_hash,
                CAST(:input_summary AS jsonb), CAST(:method_parameters AS jsonb),
                CAST(:output_summary AS jsonb), CAST(:evidence_summary AS jsonb),
                :implementation_version, :actor_id,
                clock_timestamp(), clock_timestamp()
            ) RETURNING id, public_id
            """
        ),
        {
            **context,
            "cutoff": cutoff,
            "as_of": as_of,
            "input_hash": input_hash,
            "input_summary": canonical_json,
            "method_parameters": json.dumps({"confidence_levels": configured_levels}),
            "output_summary": json.dumps(output_summary, ensure_ascii=False),
            "evidence_summary": json.dumps(
                {"failure_groups": failure_evidence, "pending_data": pending_failures > 0 or pending_total > 0},
                ensure_ascii=False,
            ),
            "implementation_version": IMPLEMENTATION_VERSION,
            "actor_id": actor_id,
        },
    ).mappings().one()
    session.execute(
        text(
            """
            INSERT INTO reliability.calculation_contributor_campaign(calculation_run_id, campaign_id)
            VALUES (:run_id, :campaign_id)
            ON CONFLICT DO NOTHING
            """
        ),
        {"run_id": run["id"], "campaign_id": campaign_id},
    )
    session.execute(
        text(
            """
            INSERT INTO reliability.current_mtbf_result(
                population_id, campaign_id, scope_id, calculation_run_id,
                exposure_seconds, relevant_failure_count, pending_block_count,
                pending_exposure_seconds, excluded_exposure_seconds,
                point_estimate_hours, no_failure_exposure_hours,
                lower_70_hours, lower_90_hours, result_payload,
                data_cutoff_at
            ) VALUES (
                NULL, :campaign_id, :scope_id, :run_id,
                :exposure_seconds, :failure_count, :pending_failure_count,
                :pending_seconds, :excluded_seconds,
                :point_estimate, :no_failure_exposure,
                :lower_70, :lower_90, CAST(:result_payload AS jsonb),
                :cutoff
            )
            ON CONFLICT (campaign_id, scope_id) WHERE campaign_id IS NOT NULL
            DO UPDATE SET
                calculation_run_id = EXCLUDED.calculation_run_id,
                exposure_seconds = EXCLUDED.exposure_seconds,
                relevant_failure_count = EXCLUDED.relevant_failure_count,
                pending_block_count = EXCLUDED.pending_block_count,
                pending_exposure_seconds = EXCLUDED.pending_exposure_seconds,
                excluded_exposure_seconds = EXCLUDED.excluded_exposure_seconds,
                point_estimate_hours = EXCLUDED.point_estimate_hours,
                no_failure_exposure_hours = EXCLUDED.no_failure_exposure_hours,
                lower_70_hours = EXCLUDED.lower_70_hours,
                lower_90_hours = EXCLUDED.lower_90_hours,
                result_payload = EXCLUDED.result_payload,
                data_cutoff_at = EXCLUDED.data_cutoff_at,
                calculated_at = clock_timestamp(),
                updated_at = clock_timestamp()
            """
        ),
        {
            "campaign_id": campaign_id,
            "scope_id": scope_id,
            "run_id": run["id"],
            "exposure_seconds": included_total,
            "failure_count": relevant_failures,
            "pending_failure_count": pending_failures,
            "pending_seconds": pending_total,
            "excluded_seconds": excluded_total,
            "point_estimate": result.point_estimate_hours,
            "no_failure_exposure": exposure_hours if relevant_failures == 0 else None,
            "lower_70": result.lower_bound(0.70),
            "lower_90": result.lower_bound(0.90),
            "result_payload": json.dumps(output_summary, ensure_ascii=False),
            "cutoff": cutoff,
        },
    )
    session.execute(
        text(
            """
            UPDATE reliability.recompute_job
            SET status = 'SUCCEEDED', completed_at = clock_timestamp(),
                locked_by = NULL, locked_at = NULL, last_error = '',
                payload = payload || jsonb_build_object(
                    'run_id', CAST(:run_id AS bigint),
                    'run_public_id', CAST(:run_public_id AS text)
                ), updated_at = clock_timestamp()
            WHERE id = :job_id
            """
        ),
        {
            "job_id": job["id"],
            "run_id": run["id"],
            "run_public_id": str(run["public_id"]),
        },
    )


def fail_job(session: Session, job: dict[str, Any], error: Exception) -> None:
    dead_letter = job["attempts"] >= job["max_attempts"]
    delay_seconds = min(300, 2 ** max(job["attempts"], 1))
    session.execute(
        text(
            """
            UPDATE reliability.recompute_job
            SET status = :status,
                available_at = CASE WHEN :dead_letter THEN available_at
                                    ELSE clock_timestamp() + make_interval(secs => :delay_seconds) END,
                completed_at = CASE WHEN :dead_letter THEN clock_timestamp() ELSE NULL END,
                locked_by = NULL, locked_at = NULL,
                last_error = :error, updated_at = clock_timestamp()
            WHERE id = :job_id
            """
        ),
        {
            "status": "DEAD_LETTER" if dead_letter else "PENDING",
            "dead_letter": dead_letter,
            "delay_seconds": delay_seconds,
            "error": str(error)[:2000],
            "job_id": job["id"],
        },
    )


def run_worker() -> None:
    settings = get_settings()
    worker_id = f"mtbf-worker-{int(time.time())}"
    logger.info("worker_started", extra={"worker_id": worker_id})
    while True:
        job: dict[str, Any] | None = None
        with SessionLocal() as session:
            try:
                with session.begin():
                    _set_worker_context(session, f"{worker_id}:claim")
                    job = claim_job(session, worker_id)
                if job:
                    with session.begin():
                        _set_worker_context(session, f"{worker_id}:job:{job['id']}")
                        process_job(session, job)
            except Exception as exc:  # noqa: BLE001 - the worker must record all failures
                logger.exception("job_failed", extra={"job_id": job and job.get("id")})
                if job:
                    with session.begin():
                        _set_worker_context(session, f"{worker_id}:failure:{job['id']}")
                        fail_job(session, job, exc)
        if not job:
            time.sleep(settings.worker_poll_seconds)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run_worker()
