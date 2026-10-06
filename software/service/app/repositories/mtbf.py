from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.errors import ApiError
from app.domain.mtbf import IMPLEMENTATION_KEY
from app.schemas.dashboard import DashboardOverview
from app.schemas.mtbf import (
    CalculationRunView,
    CampaignMtbfConfigRequest,
    CampaignMtbfConfigView,
    CampaignMtbfResultView,
    FailureBreakdownView,
    FailureGroupView,
    InterruptionUpdateRequest,
    InterruptionView,
    PerAssetExposure,
    RecalculateResponse,
    TimeBreakdownView,
    TimeIntervalDetail,
)


def _hours(seconds: Decimal | float | int | None) -> float:
    return float(seconds or 0) / 3600.0


def _etag(value: datetime) -> str:
    return f'"{value.isoformat()}"'


def _parse_if_match(value: str) -> datetime:
    normalized = value.strip()
    if normalized.startswith("W/"):
        normalized = normalized[2:]
    normalized = normalized.strip('"')
    try:
        return datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ApiError(409, "stale_revision", "If-Match不是有效的资源版本。") from exc


class MtbfRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    @staticmethod
    def _enforce_access_state(state: str, resource_name: str) -> None:
        if state == "FORBIDDEN":
            raise ApiError(403, "forbidden", f"当前用户无权访问该{resource_name}。")
        if state == "NOT_FOUND":
            raise ApiError(404, "not_found", f"未找到{resource_name}。")

    def _require_campaign_access(self, campaign_id: UUID, require_edit: bool) -> None:
        state = self.session.execute(
            text("SELECT iam.campaign_access_state(CAST(:resource_id AS uuid), :require_edit)"),
            {"resource_id": str(campaign_id), "require_edit": require_edit},
        ).scalar_one()
        self._enforce_access_state(state, "Campaign")

    def _require_interruption_access(self, interruption_id: UUID, require_edit: bool) -> None:
        state = self.session.execute(
            text("SELECT iam.interruption_access_state(CAST(:resource_id AS uuid), :require_edit)"),
            {"resource_id": str(interruption_id), "require_edit": require_edit},
        ).scalar_one()
        self._enforce_access_state(state, "中断记录")

    def _require_calculation_access(self, run_id: UUID, require_edit: bool) -> None:
        state = self.session.execute(
            text("SELECT iam.calculation_access_state(CAST(:resource_id AS uuid), :require_edit)"),
            {"resource_id": str(run_id), "require_edit": require_edit},
        ).scalar_one()
        self._enforce_access_state(state, "计算快照")

    def _campaign_scope(
        self,
        campaign_id: UUID,
        scope_code: str,
        *,
        require_edit: bool = False,
    ) -> dict:
        self._require_campaign_access(campaign_id, require_edit)
        row = self.session.execute(
            text(
                """
                SELECT c.id AS campaign_id, c.public_id AS campaign_public_id,
                       c.campaign_code, c.name AS campaign_name,
                       c.status AS campaign_status, c.asset_kind,
                       s.id AS scope_id, s.public_id AS scope_public_id,
                       s.scope_code, s.name AS scope_name
                FROM test.test_campaign c
                JOIN LATERAL (
                    SELECT s.* FROM reliability.mtbf_scope s
                    WHERE s.scope_code = :scope_code
                      AND s.asset_kind = c.asset_kind
                      AND s.status = 'PUBLISHED'
                    ORDER BY s.version DESC
                    LIMIT 1
                ) s ON true
                WHERE c.public_id = :campaign_id
                """
            ),
            {"campaign_id": str(campaign_id), "scope_code": scope_code.upper()},
        ).mappings().first()
        if not row:
            raise ApiError(404, "not_found", "未找到Campaign或对应的已发布MTBF scope。")
        return dict(row)

    def upsert_config(
        self,
        campaign_id: UUID,
        scope_code: str,
        payload: CampaignMtbfConfigRequest,
        if_match: str | None,
    ) -> CampaignMtbfConfigView:
        context = self._campaign_scope(campaign_id, scope_code, require_edit=True)
        method_id = self.session.execute(
            text(
                """
                SELECT id FROM reliability.statistics_method
                WHERE implementation_key = :implementation_key AND status = 'PUBLISHED'
                ORDER BY version DESC LIMIT 1
                """
            ),
            {"implementation_key": IMPLEMENTATION_KEY},
        ).scalar_one()
        actor_id = self.session.execute(text("SELECT iam.current_user_id()" )).scalar_one()
        target_seconds = (
            None
            if payload.target_hours is None
            else Decimal(str(payload.target_hours)) * 3600
        )
        existing = self.session.execute(
            text(
                """
                SELECT id, method_id, target_seconds, confidence_levels,
                       enabled, settings, updated_at
                FROM reliability.campaign_mtbf_config
                WHERE campaign_id = :campaign_id AND scope_id = :scope_id
                """
            ),
            context,
        ).mappings().first()
        if existing:
            same_effect = (
                existing["method_id"] == method_id
                and existing["target_seconds"] == target_seconds
                and [float(value) for value in existing["confidence_levels"]]
                == payload.confidence_levels
                and existing["enabled"] == payload.enabled
                and existing["settings"] == payload.settings
            )
            if same_effect:
                return self.get_config(campaign_id, scope_code)
            if if_match is None:
                raise ApiError(
                    428,
                    "precondition_required",
                    "修改已有配置必须携带当前ETag作为If-Match。",
                )
            if existing["updated_at"] != _parse_if_match(if_match):
                raise ApiError(409, "stale_revision", "配置已被其他请求修改，请刷新后重试。")

        parameters = {
            **context,
            "method_id": method_id,
            "target_seconds": target_seconds,
            "confidence_levels": payload.confidence_levels,
            "enabled": payload.enabled,
            "settings": json.dumps(payload.settings, ensure_ascii=False, sort_keys=True),
            "actor_id": actor_id,
        }
        if existing:
            self.session.execute(
                text(
                    """
                    UPDATE reliability.campaign_mtbf_config
                    SET method_id = :method_id,
                        target_seconds = :target_seconds,
                        confidence_levels = CAST(:confidence_levels AS numeric[]),
                        enabled = :enabled,
                        settings = CAST(:settings AS jsonb),
                        updated_by = :actor_id,
                        updated_at = clock_timestamp()
                    WHERE id = :config_id
                      AND (
                          method_id IS DISTINCT FROM :method_id
                          OR target_seconds IS DISTINCT FROM :target_seconds
                          OR confidence_levels IS DISTINCT FROM CAST(:confidence_levels AS numeric[])
                          OR enabled IS DISTINCT FROM :enabled
                          OR settings IS DISTINCT FROM CAST(:settings AS jsonb)
                      )
                    """
                ),
                {**parameters, "config_id": existing["id"]},
            )
        else:
            self.session.execute(
                text(
                    """
                    INSERT INTO reliability.campaign_mtbf_config(
                        campaign_id, scope_id, method_id, target_seconds,
                        confidence_levels, enabled, settings, created_by, updated_by
                    ) VALUES (
                        :campaign_id, :scope_id, :method_id, :target_seconds,
                        CAST(:confidence_levels AS numeric[]), :enabled,
                        CAST(:settings AS jsonb), :actor_id, :actor_id
                    )
                    """
                ),
                parameters,
            )
        return self.get_config(campaign_id, scope_code)

    def get_config(self, campaign_id: UUID, scope_code: str) -> CampaignMtbfConfigView:
        context = self._campaign_scope(campaign_id, scope_code)
        row = self.session.execute(
            text(
                """
                SELECT cfg.public_id, c.public_id AS campaign_public_id,
                       s.scope_code, m.implementation_key,
                       cfg.target_seconds, cfg.confidence_levels,
                       cfg.enabled, cfg.settings, cfg.updated_at
                FROM reliability.campaign_mtbf_config cfg
                JOIN test.test_campaign c ON c.id = cfg.campaign_id
                JOIN reliability.mtbf_scope s ON s.id = cfg.scope_id
                JOIN reliability.statistics_method m ON m.id = cfg.method_id
                WHERE cfg.campaign_id = :campaign_id AND cfg.scope_id = :scope_id
                """
            ),
            context,
        ).mappings().first()
        if not row:
            raise ApiError(404, "not_found", "该Campaign尚未启用此MTBF scope。")
        return CampaignMtbfConfigView(
            id=row["public_id"],
            campaign_id=row["campaign_public_id"],
            scope=row["scope_code"],
            method=row["implementation_key"],
            target_hours=None if row["target_seconds"] is None else _hours(row["target_seconds"]),
            confidence_levels=[float(value) for value in row["confidence_levels"]],
            enabled=row["enabled"],
            settings=row["settings"],
            updated_at=row["updated_at"],
            etag=_etag(row["updated_at"]),
        )

    def get_current_result(self, campaign_id: UUID, scope_code: str) -> CampaignMtbfResultView:
        context = self._campaign_scope(campaign_id, scope_code)
        row = self.session.execute(
            text(
                """
                SELECT c.public_id AS campaign_public_id, c.campaign_code,
                       c.name AS campaign_name, c.status AS campaign_status,
                       c.asset_kind,
                       s.scope_code, s.name AS scope_name,
                       cfg.target_seconds, cfg.enabled,
                       r.public_id AS result_public_id,
                       run.public_id AS run_public_id,
                       r.exposure_seconds, r.relevant_failure_count,
                       r.pending_block_count, r.pending_exposure_seconds,
                       r.excluded_exposure_seconds, r.point_estimate_hours,
                       r.lower_70_hours, r.lower_90_hours,
                       r.result_payload, r.calculated_at,
                       active_job.status AS active_job_status
                FROM reliability.campaign_mtbf_config cfg
                JOIN test.test_campaign c ON c.id = cfg.campaign_id
                JOIN reliability.mtbf_scope s ON s.id = cfg.scope_id
                LEFT JOIN reliability.current_mtbf_result r
                  ON r.campaign_id = cfg.campaign_id AND r.scope_id = cfg.scope_id
                LEFT JOIN reliability.calculation_run run ON run.id = r.calculation_run_id
                LEFT JOIN LATERAL (
                    SELECT j.status FROM reliability.recompute_job j
                    WHERE j.campaign_id = cfg.campaign_id AND j.scope_id = cfg.scope_id
                      AND j.status IN ('PENDING', 'PROCESSING')
                    ORDER BY j.id DESC LIMIT 1
                ) active_job ON true
                WHERE cfg.campaign_id = :campaign_id AND cfg.scope_id = :scope_id
                """
            ),
            context,
        ).mappings().first()
        if not row:
            raise ApiError(404, "not_found", "该Campaign尚未启用此MTBF scope。")
        return self._result_view(row)

    def get_time_breakdown(self, campaign_id: UUID, scope_code: str) -> TimeBreakdownView:
        result = self.get_current_result(campaign_id, scope_code)
        context = self._campaign_scope(campaign_id, scope_code)
        payload = self.session.execute(
            text(
                """
                SELECT coalesce(r.result_payload, '{}'::jsonb)
                FROM reliability.campaign_mtbf_config cfg
                LEFT JOIN reliability.current_mtbf_result r
                  ON r.campaign_id = cfg.campaign_id AND r.scope_id = cfg.scope_id
                WHERE cfg.campaign_id = :campaign_id AND cfg.scope_id = :scope_id
                """
            ),
            context,
        ).scalar_one_or_none() or {}
        details = payload.get("time_breakdown", {})
        return TimeBreakdownView(
            campaign_id=result.campaign_id,
            scope=result.scope,
            total_test_duration_hours=result.total_test_duration_hours,
            runtime_hours=result.runtime_hours,
            eligible_exposure_hours=result.eligible_exposure_hours,
            excluded_hours=result.excluded_hours,
            pending_hours=result.pending_hours,
            per_asset_exposure=result.per_asset_exposure,
            intervals=[TimeIntervalDetail.model_validate(item) for item in details.get("intervals", [])],
        )

    def get_failures(self, campaign_id: UUID, scope_code: str) -> FailureBreakdownView:
        context = self._campaign_scope(campaign_id, scope_code)
        rows = list(
            self.session.execute(
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
                    WHERE i.campaign_id = :campaign_id AND NOT i.voided
                    ORDER BY i.started_at, i.id
                    """
                ),
                context,
            ).mappings()
        )
        groups: dict[str, dict] = {}
        for row in rows:
            key = (row["failure_group_key"] or str(row["public_id"])).strip()
            item = groups.setdefault(
                key,
                {
                    "group_key": key,
                    "classification": row["classification"],
                    "review_status": row["review_status"],
                    "scope_status": row["scope_status"],
                    "counted": False,
                    "interruption_count": 0,
                    "interruption_ids": [],
                    "first_seen_at": row["started_at"],
                    "error_codes": set(),
                    "pending": False,
                },
            )
            counted = (
                row["classification"] == "FAILED"
                and row["review_status"] in {"CONFIRMED", "RECLASSIFIED"}
                and row["scope_status"] == "INCLUDED"
            )
            pending = (
                row["classification"] == "BLOCKED"
                or row["review_status"] == "PENDING"
                or row["scope_status"] == "PENDING"
            )
            item["counted"] = item["counted"] or counted
            item["pending"] = item["pending"] or pending
            item["interruption_count"] += 1
            item["interruption_ids"].append(row["public_id"])
            item["error_codes"].update(row["error_codes"])
            if pending:
                item["review_status"] = "PENDING"

        views = [
            FailureGroupView(
                **{key: value for key, value in item.items() if key not in {"pending", "error_codes"}},
                error_codes=sorted(item["error_codes"]),
            )
            for item in groups.values()
        ]
        return FailureBreakdownView(
            campaign_id=context["campaign_public_id"],
            scope=context["scope_code"],
            relevant_failure_count=sum(1 for item in groups.values() if item["counted"]),
            pending_failure_count=sum(1 for item in groups.values() if item["pending"]),
            groups=views,
        )

    def update_interruption(
        self,
        interruption_id: UUID,
        payload: InterruptionUpdateRequest,
        if_match: str,
    ) -> InterruptionView:
        self._require_interruption_access(interruption_id, require_edit=True)
        assignments: list[str] = []
        parameters: dict = {
            "interruption_id": str(interruption_id),
            "expected_updated_at": _parse_if_match(if_match),
        }
        for field in ("classification", "review_status", "failure_group_key", "relevance_reason"):
            if field in payload.model_fields_set:
                assignments.append(f"{field} = :{field}")
                parameters[field] = getattr(payload, field)
        assignments.extend(
            [
                "confirmed_by = CASE WHEN :review_changed THEN iam.current_user_id() ELSE confirmed_by END",
                "confirmed_at = CASE WHEN :review_changed THEN clock_timestamp() ELSE confirmed_at END",
                "updated_at = clock_timestamp()",
            ]
        )
        parameters["review_changed"] = (
            "review_status" in payload.model_fields_set
            and payload.review_status in {"CONFIRMED", "RECLASSIFIED"}
        )
        try:
            row = self.session.execute(
                text(
                    f"""
                    UPDATE reliability.interruption
                    SET {', '.join(assignments)}
                    WHERE public_id = :interruption_id
                      AND updated_at = :expected_updated_at
                    RETURNING public_id, classification, review_status,
                              failure_group_key, relevance_reason, updated_at
                    """
                ),
                parameters,
            ).mappings().first()
        except IntegrityError as exc:
            raise ApiError(422, "validation_error", "故障分类与错误码或原因不符合数据库规则。") from exc
        if not row:
            current = self.session.execute(
                text(
                    """
                    SELECT public_id, classification, review_status,
                           failure_group_key, relevance_reason, updated_at
                    FROM reliability.interruption
                    WHERE public_id = :interruption_id
                    """
                ),
                parameters,
            ).mappings().first()
            if current:
                same_effect = all(
                    current[field] == getattr(payload, field)
                    for field in payload.model_fields_set
                )
                if same_effect:
                    return InterruptionView(
                        id=current["public_id"],
                        classification=current["classification"],
                        review_status=current["review_status"],
                        failure_group_key=current["failure_group_key"],
                        relevance_reason=current["relevance_reason"],
                        updated_at=current["updated_at"],
                        etag=_etag(current["updated_at"]),
                    )
                raise ApiError(409, "stale_revision", "中断记录已被其他请求修改，请刷新后重试。")
            raise ApiError(404, "not_found", "未找到中断记录，或当前用户没有访问权限。")
        return InterruptionView(
            id=row["public_id"],
            classification=row["classification"],
            review_status=row["review_status"],
            failure_group_key=row["failure_group_key"],
            relevance_reason=row["relevance_reason"],
            updated_at=row["updated_at"],
            etag=_etag(row["updated_at"]),
        )

    def queue_recalculation(
        self,
        campaign_id: UUID,
        scope_code: str,
        reason: str,
        idempotency_key: str,
    ) -> RecalculateResponse:
        context = self._campaign_scope(campaign_id, scope_code, require_edit=True)
        enabled = self.session.execute(
            text(
                """
                SELECT enabled FROM reliability.campaign_mtbf_config
                WHERE campaign_id = :campaign_id AND scope_id = :scope_id
                """
            ),
            context,
        ).scalar_one_or_none()
        if enabled is None:
            raise ApiError(404, "not_found", "该Campaign尚未配置此MTBF scope。")
        if not enabled:
            raise ApiError(409, "scope_disabled", "该Campaign的MTBF scope已停用。")
        job = self.session.execute(
            text(
                """
                SELECT (reliability.enqueue_campaign_recompute(
                    :campaign_id, :scope_id, :reason, :idempotency_key
                )).*
                """
            ),
            {**context, "reason": reason, "idempotency_key": idempotency_key},
        ).mappings().one()
        run_id = (job["payload"] or {}).get("run_public_id")
        return RecalculateResponse(job_id=job["public_id"], status=job["status"], run_id=run_id)

    def get_calculation_run(self, run_id: UUID) -> CalculationRunView:
        self._require_calculation_access(run_id, require_edit=False)
        row = self.session.execute(
            text(
                """
                SELECT r.public_id, c.public_id AS campaign_public_id,
                       s.scope_code, r.status, r.data_cutoff_at,
                       r.knowledge_as_of_at, r.implementation_version,
                       r.input_hash, r.input_summary, r.output_summary,
                       r.evidence_summary, r.error_detail,
                       r.created_at, r.completed_at
                FROM reliability.calculation_run r
                LEFT JOIN test.test_campaign c ON c.id = r.campaign_id
                JOIN reliability.mtbf_scope s ON s.id = r.scope_id
                WHERE r.public_id = :run_id
                """
            ),
            {"run_id": str(run_id)},
        ).mappings().first()
        if not row:
            raise ApiError(404, "not_found", "未找到计算快照，或当前用户没有访问权限。")
        return CalculationRunView(
            id=row["public_id"],
            campaign_id=row["campaign_public_id"],
            scope=row["scope_code"],
            status=row["status"],
            data_cutoff_at=row["data_cutoff_at"],
            knowledge_as_of_at=row["knowledge_as_of_at"],
            implementation_version=row["implementation_version"],
            input_hash=row["input_hash"],
            input_summary=row["input_summary"],
            output_summary=row["output_summary"],
            evidence_summary=row["evidence_summary"],
            error_detail=row["error_detail"],
            created_at=row["created_at"],
            completed_at=row["completed_at"],
        )

    def dashboard_overview(self, asset_kind: str | None = None) -> DashboardOverview:
        configured = list(
            self.session.execute(
                text(
                    """
                    SELECT c.public_id AS campaign_id, s.scope_code
                    FROM reliability.campaign_mtbf_config cfg
                    JOIN test.test_campaign c ON c.id = cfg.campaign_id
                    JOIN reliability.mtbf_scope s ON s.id = cfg.scope_id
                    WHERE cfg.enabled
                      AND (
                          CAST(:asset_kind AS text) IS NULL
                          OR c.asset_kind = CAST(:asset_kind AS text)
                      )
                    ORDER BY (c.status = 'ACTIVE') DESC, c.updated_at DESC
                    LIMIT 20
                    """
                ),
                {"asset_kind": asset_kind},
            ).mappings()
        )
        campaigns = [self.get_current_result(row["campaign_id"], row["scope_code"]) for row in configured]
        calculated = [item.calculated_at for item in campaigns if item.calculated_at]
        return DashboardOverview(
            configured_campaign_count=len(campaigns),
            active_campaign_count=sum(1 for item in campaigns if item.campaign_status == "ACTIVE"),
            eligible_exposure_hours=sum(item.eligible_exposure_hours for item in campaigns),
            relevant_failure_count=sum(item.relevant_failure_count for item in campaigns),
            pending_failure_count=sum(item.pending_failure_count for item in campaigns),
            pending_exposure_hours=sum(item.pending_hours for item in campaigns),
            last_calculated_at=max(calculated) if calculated else None,
            campaigns=campaigns,
        )

    @staticmethod
    def _result_view(row: dict) -> CampaignMtbfResultView:
        payload = row["result_payload"] or {}
        total_hours = float(payload.get("total_test_duration_hours", 0))
        runtime_hours = float(payload.get("runtime_hours", 0))
        eligible = _hours(row["exposure_seconds"])
        target = None if row["target_seconds"] is None else _hours(row["target_seconds"])
        progress = None if target is None else eligible / target * 100
        confidence = {
            str(key): float(value)
            for key, value in (payload.get("confidence_bounds") or {}).items()
        }
        lower_70 = float(row["lower_70_hours"] or confidence.get("0.7", 0))
        lower_90 = float(row["lower_90_hours"] or confidence.get("0.9", 0))
        per_asset = [
            PerAssetExposure.model_validate(item)
            for item in payload.get("per_asset_exposure", [])
        ]
        return CampaignMtbfResultView(
            campaign_id=row["campaign_public_id"],
            campaign_code=row["campaign_code"],
            campaign_name=row["campaign_name"],
            campaign_status=row["campaign_status"],
            asset_kind=row["asset_kind"],
            scope=row["scope_code"],
            scope_name=row["scope_name"],
            target_hours=target,
            progress_percent=progress,
            total_test_duration_hours=total_hours,
            runtime_hours=runtime_hours,
            eligible_exposure_hours=eligible,
            relevant_failure_count=int(row["relevant_failure_count"] or 0),
            pending_failure_count=int(row["pending_block_count"] or 0),
            mtbf_point_estimate_hours=(
                float(row["point_estimate_hours"])
                if row["point_estimate_hours"] is not None
                else None
            ),
            point_estimate_status=payload.get("point_estimate_status", "NO_EXPOSURE"),
            mtbf_lower_70_hours=lower_70,
            mtbf_lower_90_hours=lower_90,
            confidence_bounds=confidence or {"0.7": lower_70, "0.9": lower_90},
            excluded_hours=_hours(row["excluded_exposure_seconds"]),
            pending_hours=_hours(row["pending_exposure_seconds"]),
            per_asset_exposure=per_asset,
            calculation_status=row["active_job_status"] or ("CURRENT" if row["result_public_id"] else "NOT_CALCULATED"),
            run_id=row["run_public_id"],
            calculated_at=row["calculated_at"],
        )
