from __future__ import annotations

import hashlib
import json
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.errors import ApiError
from app.schemas.ingestion import (
    ArtifactMetadataRequest,
    EventBatchRequest,
    ExecutionRegisterRequest,
    ExecutionRegistration,
    FinishExecutionRequest,
    HeartbeatRequest,
    IngestionResult,
    TelemetryBatchRequest,
)


STATUS_MAP = {
    "running": "RUNNING",
    "passed": "COMPLETED",
    "failed": "FAILED",
    "blocked": "BLOCKED",
}


class IngestionRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    @staticmethod
    def payload_hash(payload: Any) -> str:
        data = payload.model_dump(mode="json")
        encoded = json.dumps(
            data, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _source(self) -> dict[str, Any]:
        row = self.session.execute(
            text(
                """
                SELECT source.id, source.public_id, source.source_code
                FROM integration.ingestion_source source
                WHERE source.id = iam.current_ingestion_source_id()
                  AND source.status = 'ACTIVE'
                """
            )
        ).mappings().first()
        if row is None:
            raise ApiError(
                403, "ingestion_source_forbidden", "服务主体没有可用采集源。"
            )
        return dict(row)

    def _claim(
        self,
        *,
        source_id: int,
        request_kind: str,
        idempotency_key: str,
        producer_key: str,
        payload_hash: str,
    ) -> tuple[int | None, dict[str, Any] | None]:
        receipt_id = self.session.execute(
            text(
                """
                INSERT INTO integration.ingestion_receipt(
                    source_id, idempotency_key, payload_hash, request_kind,
                    producer_key, status
                ) VALUES (
                    :source_id, :idempotency_key, :payload_hash, :request_kind,
                    :producer_key, 'ACCEPTED'
                )
                ON CONFLICT DO NOTHING
                RETURNING id
                """
            ),
            {
                "source_id": source_id,
                "idempotency_key": idempotency_key,
                "payload_hash": payload_hash,
                "request_kind": request_kind,
                "producer_key": producer_key,
            },
        ).scalar_one_or_none()
        if receipt_id is not None:
            return int(receipt_id), None

        row = self.session.execute(
            text(
                """
                SELECT id, payload_hash, request_kind, producer_key,
                       http_status, response_body
                FROM integration.ingestion_receipt
                WHERE source_id = :source_id
                  AND (
                    idempotency_key = :idempotency_key
                    OR (request_kind = :request_kind
                        AND producer_key = :producer_key)
                  )
                ORDER BY id
                LIMIT 1
                """
            ),
            {
                "source_id": source_id,
                "idempotency_key": idempotency_key,
                "request_kind": request_kind,
                "producer_key": producer_key,
            },
        ).mappings().one()
        if (
            row["payload_hash"] != payload_hash
            or row["request_kind"] != request_kind
            or row["producer_key"] != producer_key
        ):
            raise ApiError(
                409,
                "idempotency_conflict",
                "Idempotency-Key或producer_key已被不同请求使用。",
            )
        if row["response_body"] is None:
            raise ApiError(
                409,
                "ingestion_in_progress",
                "同一幂等请求正在处理中，请稍后重试。",
            )
        response = dict(row["response_body"])
        response["replayed"] = True
        return None, response

    def _complete_receipt(
        self,
        receipt_id: int,
        response: dict[str, Any],
        *,
        entity_type: str,
        entity_public_id: UUID | None,
        batch_id: int | None = None,
        http_status: int = 200,
    ) -> None:
        self.session.execute(
            text(
                """
                UPDATE integration.ingestion_receipt
                SET batch_id = :batch_id,
                    entity_type = :entity_type,
                    entity_public_id = :entity_public_id,
                    http_status = :http_status,
                    response_body = CAST(:response_body AS jsonb)
                WHERE id = :receipt_id
                """
            ),
            {
                "receipt_id": receipt_id,
                "batch_id": batch_id,
                "entity_type": entity_type,
                "entity_public_id": (
                    str(entity_public_id) if entity_public_id else None
                ),
                "http_status": http_status,
                "response_body": json.dumps(response, ensure_ascii=False),
            },
        )

    def _execution(self, execution_id: UUID) -> dict[str, Any]:
        row = self.session.execute(
            text(
                """
                SELECT execution.id, execution.id AS execution_id,
                       execution.public_id,
                       execution.execution_code, execution.campaign_id,
                       execution.asset_id, execution.cycle_id,
                       execution.configuration_snapshot_id,
                       execution.normalized_started_at,
                       execution.normalized_ended_at, execution.status,
                       segment.id AS segment_id
                FROM test.test_execution execution
                JOIN test.analysis_segment segment
                  ON segment.id = execution.ingestion_segment_id
                WHERE execution.public_id = :execution_id
                  AND execution.source_id = iam.current_ingestion_source_id()
                """
            ),
            {"execution_id": str(execution_id)},
        ).mappings().first()
        if row is None:
            raise ApiError(
                404, "not_found", "未找到属于当前采集源的execution。"
            )
        return dict(row)

    def execution_id_by_producer(
        self, producer_execution_key: str
    ) -> UUID:
        row = self.session.execute(
            text(
                """
                SELECT execution.public_id
                FROM test.test_execution execution
                WHERE execution.source_execution_key =
                      :producer_execution_key
                  AND execution.source_id =
                      iam.current_ingestion_source_id()
                """
            ),
            {"producer_execution_key": producer_execution_key},
        ).scalar_one_or_none()
        if row is None:
            raise ApiError(
                404,
                "not_found",
                "当前采集源下不存在该producer execution key。",
            )
        return UUID(str(row))

    def _start_batch(
        self,
        *,
        source_id: int,
        execution_db_id: int,
        producer_key: str,
        batch_kind: str,
        payload_hash: str,
        item_count: int,
    ) -> int:
        return int(
            self.session.execute(
                text(
                    """
                    INSERT INTO integration.ingestion_batch(
                        source_id, producer_batch_key, execution_id, batch_kind,
                        payload_hash, item_count, status
                    ) VALUES (
                        :source_id, :producer_key, :execution_id, :batch_kind,
                        :payload_hash, :item_count, 'RECEIVING'
                    )
                    RETURNING id
                    """
                ),
                {
                    "source_id": source_id,
                    "producer_key": producer_key,
                    "execution_id": execution_db_id,
                    "batch_kind": batch_kind,
                    "payload_hash": payload_hash,
                    "item_count": item_count,
                },
            ).scalar_one()
        )

    def _finish_batch(self, batch_id: int, accepted_count: int) -> None:
        self.session.execute(
            text(
                """
                UPDATE integration.ingestion_batch
                SET accepted_count = :accepted_count,
                    status = CASE
                        WHEN :accepted_count = item_count THEN 'ACCEPTED'
                        ELSE 'PARTIAL'
                    END,
                    completed_at = clock_timestamp()
                WHERE id = :batch_id
                """
            ),
            {"batch_id": batch_id, "accepted_count": accepted_count},
        )

    def register_execution(
        self,
        payload: ExecutionRegisterRequest,
        idempotency_key: str,
    ) -> ExecutionRegistration:
        source = self._source()
        digest = self.payload_hash(payload)
        receipt_id, replay = self._claim(
            source_id=source["id"],
            request_kind="EXECUTION_REGISTER",
            idempotency_key=idempotency_key,
            producer_key=payload.producer_key,
            payload_hash=digest,
        )
        if replay is not None:
            return ExecutionRegistration.model_validate(replay)

        context = self.session.execute(
            text(
                """
                SELECT campaign.id AS campaign_id,
                       cycle.id AS cycle_id,
                       segment.id AS segment_id,
                       asset.id AS asset_id,
                       configuration.id AS configuration_id,
                       version.id AS test_case_version_id,
                       station.id AS station_id,
                       campaign.asset_kind,
                       station.station_type
                FROM test.test_campaign campaign
                JOIN test.test_cycle cycle
                  ON cycle.public_id = :cycle_id
                 AND cycle.campaign_id = campaign.id
                JOIN test.asset asset
                  ON asset.public_id = :asset_id
                 AND asset.id = cycle.asset_id
                JOIN test.configuration_snapshot configuration
                  ON configuration.public_id = :configuration_id
                 AND configuration.asset_id = asset.id
                JOIN test.analysis_segment segment
                  ON segment.public_id = :segment_id
                 AND segment.campaign_id = campaign.id
                 AND segment.cycle_id = cycle.id
                 AND segment.configuration_snapshot_id = configuration.id
                JOIN catalog.test_case_version version
                  ON version.public_id = :test_case_version_id
                 AND version.status = 'PUBLISHED'
                JOIN test.station station
                  ON station.public_id = :station_id
                 AND station.enabled
                WHERE campaign.public_id = :campaign_id
                  AND campaign.status IN ('PLANNED', 'ACTIVE', 'PAUSED')
                """
            ),
            {
                "campaign_id": str(payload.campaign_id),
                "cycle_id": str(payload.cycle_id),
                "segment_id": str(payload.segment_id),
                "asset_id": str(payload.asset_id),
                "configuration_id": str(payload.configuration_id),
                "test_case_version_id": str(payload.test_case_version_id),
                "station_id": str(payload.station_id),
            },
        ).mappings().first()
        if context is None:
            raise ApiError(
                422,
                "context_mismatch",
                "campaign/cycle/asset/config/test-case/segment/station上下文不一致。",
            )
        if context["station_type"] not in {
            context["asset_kind"],
            "SHARED",
        }:
            raise ApiError(
                422,
                "station_mismatch",
                "station类型与测试对象类型不匹配。",
            )

        existing = self.session.execute(
            text(
                """
                SELECT execution.public_id AS execution_id,
                       execution.execution_code,
                       stage.public_id AS stage_id,
                       execution.status, result.source_status,
                       campaign.public_id AS campaign_public_id,
                       cycle.public_id AS cycle_public_id,
                       asset.public_id AS asset_public_id,
                       configuration.public_id AS configuration_public_id,
                       version.public_id AS version_public_id,
                       station.public_id AS station_public_id,
                       segment.public_id AS segment_public_id
                FROM test.test_execution execution
                JOIN test.execution_stage stage
                  ON stage.execution_id = execution.id
                 AND stage.sequence_no = 1
                JOIN test.test_campaign campaign
                  ON campaign.id = execution.campaign_id
                JOIN test.test_cycle cycle ON cycle.id = execution.cycle_id
                JOIN test.asset asset ON asset.id = execution.asset_id
                JOIN test.configuration_snapshot configuration
                  ON configuration.id = execution.configuration_snapshot_id
                JOIN catalog.test_case_version version
                  ON version.id = execution.test_case_version_id
                JOIN test.station station ON station.id = execution.station_id
                JOIN test.analysis_segment segment
                  ON segment.id = execution.ingestion_segment_id
                LEFT JOIN test.execution_result result
                  ON result.execution_id = execution.id
                WHERE execution.source_id = :source_id
                  AND execution.source_execution_key = :producer_key
                """
            ),
            {
                "source_id": source["id"],
                "producer_key": payload.producer_key,
            },
        ).mappings().first()
        if existing is None:
            execution = self.session.execute(
                text(
                    """
                    INSERT INTO test.test_execution(
                        execution_code, campaign_id, cycle_id, asset_id,
                        configuration_snapshot_id, test_case_version_id,
                        station_id, ingestion_segment_id, source_id,
                        source_execution_key, status,
                        source_time, received_at, normalized_started_at,
                        code_timestamp, clock_quality, data_quality
                    ) VALUES (
                        'GENERATED', :campaign_id, :cycle_id, :asset_id,
                        :configuration_id, :test_case_version_id, :station_id,
                        :segment_id, :source_id, :producer_key, 'RUNNING', :started_at,
                        clock_timestamp(), :started_at, :started_at,
                        'VALID', 'VALID'
                    )
                    RETURNING id, public_id, execution_code
                    """
                ),
                {
                    **dict(context),
                    "source_id": source["id"],
                    "producer_key": payload.producer_key,
                    "started_at": payload.started_at,
                },
            ).mappings().one()
            self.session.execute(
                text(
                    """
                    INSERT INTO test.execution_result(
                        execution_id, source_status, outcome, termination_kind,
                        reported_duration_seconds, archive_status,
                        normalization_flags, source_created_at, source_updated_at
                    ) VALUES (
                        :execution_id, 'running', 'NOT_EVALUATED', 'RUNNING',
                        0, 'PENDING', '[]'::jsonb, :started_at, :started_at
                    )
                    """
                ),
                {
                    "execution_id": execution["id"],
                    "started_at": payload.started_at,
                },
            )
            stage = self.session.execute(
                text(
                    """
                    INSERT INTO test.execution_stage(
                        campaign_id, execution_id, stage_code, stage_name,
                        sequence_no, status, source_time, received_at,
                        normalized_started_at, clock_quality
                    ) VALUES (
                        :campaign_id, :execution_id, :stage_code, :stage_name,
                        1, 'RUNNING', :started_at, clock_timestamp(),
                        :started_at, 'VALID'
                    )
                    RETURNING public_id
                    """
                ),
                {
                    "campaign_id": context["campaign_id"],
                    "execution_id": execution["id"],
                    "stage_code": payload.stage_code,
                    "stage_name": payload.stage_name,
                    "started_at": payload.started_at,
                },
            ).scalar_one()
            response = ExecutionRegistration(
                execution_id=execution["public_id"],
                execution_code=execution["execution_code"],
                stage_id=stage,
                status="RUNNING",
                source_status="running",
            )
        else:
            bound_context = (
                existing["campaign_public_id"],
                existing["cycle_public_id"],
                existing["asset_public_id"],
                existing["configuration_public_id"],
                existing["version_public_id"],
                existing["station_public_id"],
                existing["segment_public_id"],
            )
            requested_context = (
                payload.campaign_id,
                payload.cycle_id,
                payload.asset_id,
                payload.configuration_id,
                payload.test_case_version_id,
                payload.station_id,
                payload.segment_id,
            )
            if bound_context != requested_context:
                raise ApiError(
                    409,
                    "producer_key_conflict",
                    "producer execution key已绑定到不同上下文。",
                )
            expected = {
                "RUNNING",
            }
            if existing["status"] not in expected:
                raise ApiError(
                    409,
                    "execution_state_conflict",
                    "producer execution key已绑定到非运行状态execution。",
                )
            response = ExecutionRegistration(
                execution_id=existing["execution_id"],
                execution_code=existing["execution_code"],
                stage_id=existing["stage_id"],
                status="RUNNING",
                source_status="running",
            )
        body = response.model_dump(mode="json")
        assert receipt_id is not None
        self._complete_receipt(
            receipt_id,
            body,
            entity_type="test.test_execution",
            entity_public_id=response.execution_id,
            http_status=201,
        )
        return response

    def heartbeat(
        self,
        execution_id: UUID,
        payload: HeartbeatRequest,
        idempotency_key: str,
    ) -> IngestionResult:
        source = self._source()
        execution = self._execution(execution_id)
        digest = self.payload_hash(payload)
        receipt_id, replay = self._claim(
            source_id=source["id"],
            request_kind="HEARTBEAT",
            idempotency_key=idempotency_key,
            producer_key=payload.producer_key,
            payload_hash=digest,
        )
        if replay is not None:
            return IngestionResult.model_validate(replay)
        event_id = self.session.execute(
            text(
                """
                INSERT INTO test.test_event(
                    campaign_id, asset_id, cycle_id, execution_id,
                    event_type, source_time, normalized_time,
                    clock_quality, data_quality, source_id, source_event_key,
                    payload
                ) VALUES (
                    :campaign_id, :asset_id, :cycle_id, :execution_id,
                    'HEARTBEAT', :occurred_at, :occurred_at,
                    :clock_quality, :data_quality, :source_id, :producer_key,
                    CAST(:payload AS jsonb)
                )
                RETURNING public_id
                """
            ),
            {
                **execution,
                "occurred_at": payload.occurred_at,
                "clock_quality": payload.clock_quality,
                "data_quality": payload.data_quality,
                "source_id": source["id"],
                "producer_key": payload.producer_key,
                "payload": json.dumps(
                    {
                        **payload.payload,
                        "source_status": payload.source_status,
                    },
                    ensure_ascii=False,
                ),
            },
        ).scalar_one()
        mapped = STATUS_MAP[payload.source_status]
        self.session.execute(
            text(
                """
                UPDATE test.test_execution
                SET status = :status, source_time = :occurred_at,
                    received_at = clock_timestamp()
                WHERE id = :execution_id
                """
            ),
            {
                "status": mapped,
                "occurred_at": payload.occurred_at,
                "execution_id": execution["id"],
            },
        )
        self.session.execute(
            text(
                """
                UPDATE test.execution_result
                SET source_status = :source_status,
                    source_updated_at = :occurred_at,
                    last_data_at = :occurred_at
                WHERE execution_id = :execution_id
                """
            ),
            {
                "source_status": payload.source_status,
                "occurred_at": payload.occurred_at,
                "execution_id": execution["id"],
            },
        )
        response = IngestionResult(
            execution_id=execution_id,
            accepted_count=1,
            event_ids=[event_id],
            status=mapped,
            source_status=payload.source_status,
        )
        assert receipt_id is not None
        self._complete_receipt(
            receipt_id,
            response.model_dump(mode="json"),
            entity_type="test.test_event",
            entity_public_id=event_id,
        )
        return response

    def ingest_events(
        self,
        execution_id: UUID,
        payload: EventBatchRequest,
        idempotency_key: str,
    ) -> IngestionResult:
        source = self._source()
        execution = self._execution(execution_id)
        digest = self.payload_hash(payload)
        receipt_id, replay = self._claim(
            source_id=source["id"],
            request_kind="EVENT_BATCH",
            idempotency_key=idempotency_key,
            producer_key=payload.producer_key,
            payload_hash=digest,
        )
        if replay is not None:
            return IngestionResult.model_validate(replay)
        batch_id = self._start_batch(
            source_id=source["id"],
            execution_db_id=execution["id"],
            producer_key=payload.producer_key,
            batch_kind="EVENTS",
            payload_hash=digest,
            item_count=len(payload.events),
        )
        event_ids: list[UUID] = []
        for item in payload.events:
            event_ids.append(
                self.session.execute(
                    text(
                        """
                        INSERT INTO test.test_event(
                            campaign_id, asset_id, cycle_id, execution_id,
                            event_type, source_time, normalized_time,
                            clock_quality, data_quality, source_id,
                            source_event_key, payload
                        ) VALUES (
                            :campaign_id, :asset_id, :cycle_id, :execution_id,
                            :event_type, :occurred_at, :occurred_at,
                            :clock_quality, :data_quality, :source_id,
                            :producer_event_key, CAST(:payload AS jsonb)
                        )
                        RETURNING public_id
                        """
                    ),
                    {
                        **execution,
                        **item.model_dump(exclude={"payload"}),
                        "source_id": source["id"],
                        "payload": json.dumps(
                            item.payload, ensure_ascii=False
                        ),
                    },
                ).scalar_one()
            )
        self._finish_batch(batch_id, len(event_ids))
        response = IngestionResult(
            execution_id=execution_id,
            accepted_count=len(event_ids),
            event_ids=event_ids,
            status=execution["status"],
        )
        assert receipt_id is not None
        self._complete_receipt(
            receipt_id,
            response.model_dump(mode="json"),
            entity_type="integration.ingestion_batch",
            entity_public_id=None,
            batch_id=batch_id,
        )
        return response

    def ingest_telemetry(
        self,
        execution_id: UUID,
        payload: TelemetryBatchRequest,
        idempotency_key: str,
    ) -> IngestionResult:
        source = self._source()
        execution = self._execution(execution_id)
        stage = self.session.execute(
            text(
                """
                SELECT id
                FROM test.execution_stage
                WHERE public_id = :stage_id
                  AND execution_id = :execution_id
                  AND campaign_id = :campaign_id
                """
            ),
            {
                "stage_id": str(payload.stage_id),
                "execution_id": execution["id"],
                "campaign_id": execution["campaign_id"],
            },
        ).scalar_one_or_none()
        if stage is None:
            raise ApiError(
                422, "context_mismatch", "stage不属于指定execution。"
            )
        digest = self.payload_hash(payload)
        receipt_id, replay = self._claim(
            source_id=source["id"],
            request_kind="TELEMETRY_BATCH",
            idempotency_key=idempotency_key,
            producer_key=payload.producer_key,
            payload_hash=digest,
        )
        if replay is not None:
            return IngestionResult.model_validate(replay)
        batch_id = self._start_batch(
            source_id=source["id"],
            execution_db_id=execution["id"],
            producer_key=payload.producer_key,
            batch_kind="TELEMETRY",
            payload_hash=digest,
            item_count=sum(len(item.points) for item in payload.series),
        )
        series_ids: list[UUID] = []
        accepted_points = 0
        for series in payload.series:
            metric = self.session.execute(
                text(
                    """
                    SELECT version.id, version.canonical_unit
                    FROM catalog.metric_version version
                    JOIN catalog.metric_definition definition
                      ON definition.id = version.metric_definition_id
                    WHERE definition.metric_code = :metric_code
                      AND version.version = :metric_version
                      AND definition.enabled
                      AND version.status = 'PUBLISHED'
                      AND version.value_type = 'NUMBER'
                    """
                ),
                {
                    "metric_code": series.metric_code,
                    "metric_version": series.metric_version,
                },
            ).mappings().first()
            if metric is None:
                raise ApiError(
                    422,
                    "metric_mismatch",
                    f"未知或未发布指标: {series.metric_code}@{series.metric_version}",
                )
            if metric["canonical_unit"] != series.canonical_unit:
                raise ApiError(
                    422,
                    "unit_mismatch",
                    (
                        f"{series.metric_code}要求规范单位"
                        f"{metric['canonical_unit']}；采集端必须在发送前完成rad转换。"
                    ),
                )
            series_row = self.session.execute(
                text(
                    """
                    INSERT INTO health.metric_series(
                        campaign_id, asset_id, cycle_id, segment_id,
                        execution_id, stage_id, configuration_snapshot_id,
                        metric_version_id, subject_code, series_kind,
                        cycle_index, started_at, ended_at, point_count,
                        sampling_interval_ms, downsample_method, quality_status,
                        raw_data_reference, source_id, source_series_key
                    ) VALUES (
                        :campaign_id, :asset_id, :cycle_id, :segment_id,
                        :execution_id, :stage_id, :configuration_id,
                        :metric_version_id, :subject_code, :series_kind,
                        :cycle_index, :started_at, :ended_at, :point_count,
                        :sampling_interval_ms, :downsample_method,
                        :quality_status, CAST(:raw_data_reference AS jsonb),
                        :source_id, :source_series_key
                    )
                    RETURNING id, public_id
                    """
                ),
                {
                    **execution,
                    "segment_id": execution["segment_id"],
                    "stage_id": stage,
                    "configuration_id": execution[
                        "configuration_snapshot_id"
                    ],
                    "metric_version_id": metric["id"],
                    "subject_code": series.subject_code,
                    "series_kind": series.series_kind,
                    "cycle_index": series.cycle_index,
                    "started_at": series.started_at,
                    "ended_at": series.ended_at,
                    "point_count": len(series.points),
                    "sampling_interval_ms": series.sampling_interval_ms,
                    "downsample_method": series.downsample_method,
                    "quality_status": series.quality_status,
                    "raw_data_reference": json.dumps(
                        series.raw_data_reference, ensure_ascii=False
                    ),
                    "source_id": source["id"],
                    "source_series_key": series.producer_series_key,
                },
            ).mappings().one()
            series_ids.append(series_row["public_id"])
            for point in series.points:
                self.session.execute(
                    text(
                        """
                        INSERT INTO health.metric_observation(
                            campaign_id, metric_version_id, asset_id, cycle_id,
                            segment_id, execution_id, stage_id,
                            configuration_snapshot_id, series_id, sample_index,
                            observed_at, canonical_numeric_value,
                            original_numeric_value, original_unit,
                            conversion_method, quality_status, source_kind
                        ) VALUES (
                            :campaign_id, :metric_version_id, :asset_id,
                            :cycle_id, :segment_id, :execution_id, :stage_id,
                            :configuration_id, :series_id, :sample_index,
                            :observed_at, :value, :value, :unit,
                            'identity:collector-normalized', :quality_status,
                            'NATIVE'
                        )
                        """
                    ),
                    {
                        **execution,
                        "metric_version_id": metric["id"],
                        "segment_id": execution["segment_id"],
                        "stage_id": stage,
                        "configuration_id": execution[
                            "configuration_snapshot_id"
                        ],
                        "series_id": series_row["id"],
                        "sample_index": point.sample_index,
                        "observed_at": point.observed_at,
                        "value": point.value,
                        "unit": series.canonical_unit,
                        "quality_status": point.quality_status,
                    },
                )
                accepted_points += 1
        self._finish_batch(batch_id, accepted_points)
        response = IngestionResult(
            execution_id=execution_id,
            accepted_count=accepted_points,
            series_ids=series_ids,
            status=execution["status"],
        )
        assert receipt_id is not None
        self._complete_receipt(
            receipt_id,
            response.model_dump(mode="json"),
            entity_type="integration.ingestion_batch",
            entity_public_id=None,
            batch_id=batch_id,
        )
        return response

    def register_artifact(
        self,
        execution_id: UUID,
        payload: ArtifactMetadataRequest,
        idempotency_key: str,
    ) -> IngestionResult:
        source = self._source()
        execution = self._execution(execution_id)
        digest = self.payload_hash(payload)
        receipt_id, replay = self._claim(
            source_id=source["id"],
            request_kind="ARTIFACT_METADATA",
            idempotency_key=idempotency_key,
            producer_key=payload.producer_key,
            payload_hash=digest,
        )
        if replay is not None:
            return IngestionResult.model_validate(replay)
        batch_id = self._start_batch(
            source_id=source["id"],
            execution_db_id=execution["id"],
            producer_key=payload.producer_key,
            batch_kind=(
                "MANIFEST"
                if "MANIFEST" in payload.artifact_kind.upper()
                else "EVIDENCE"
            ),
            payload_hash=digest,
            item_count=1,
        )
        artifact = self.session.execute(
            text(
                """
                INSERT INTO integration.artifact(
                    campaign_id, asset_id, cycle_id, execution_id, kind,
                    availability_status, object_key, source_location,
                    file_name, mime_type, size_bytes, sha256, source_id,
                    source_artifact_key, metadata
                ) VALUES (
                    :campaign_id, :asset_id, :cycle_id, :execution_id, :kind,
                    :availability_status, :object_key, :source_location,
                    :file_name, :mime_type, :size_bytes, :sha256, :source_id,
                    :source_artifact_key, CAST(:metadata AS jsonb)
                )
                RETURNING id, public_id
                """
            ),
            {
                **execution,
                "kind": payload.artifact_kind,
                "availability_status": payload.availability_status,
                "object_key": payload.object_key,
                "source_location": payload.source_location,
                "file_name": payload.file_name,
                "mime_type": payload.mime_type,
                "size_bytes": payload.size_bytes,
                "sha256": payload.sha256,
                "source_id": source["id"],
                "source_artifact_key": payload.producer_key,
                "metadata": json.dumps(
                    payload.metadata, ensure_ascii=False
                ),
            },
        ).mappings().one()
        trajectory_id = None
        if payload.trajectory_key is not None:
            trajectory_id = self.session.execute(
                text(
                    """
                    INSERT INTO integration.trajectory_version(
                        source_id, campaign_id, asset_id, execution_id,
                        artifact_id, trajectory_key, version, file_sha256,
                        file_size_bytes, coordinate_frame, point_count, metadata
                    ) VALUES (
                        :source_id, :campaign_id, :asset_id, :execution_id,
                        :artifact_id, :trajectory_key, :version, :file_sha256,
                        :file_size_bytes, :coordinate_frame, :point_count,
                        CAST(:metadata AS jsonb)
                    )
                    RETURNING public_id
                    """
                ),
                {
                    **execution,
                    "source_id": source["id"],
                    "artifact_id": artifact["id"],
                    "trajectory_key": payload.trajectory_key,
                    "version": payload.trajectory_version,
                    "file_sha256": payload.sha256,
                    "file_size_bytes": payload.size_bytes,
                    "coordinate_frame": payload.coordinate_frame,
                    "point_count": payload.trajectory_point_count,
                    "metadata": json.dumps(
                        payload.metadata, ensure_ascii=False
                    ),
                },
            ).scalar_one()
        self._finish_batch(batch_id, 1)
        response = IngestionResult(
            execution_id=execution_id,
            accepted_count=1,
            artifact_id=artifact["public_id"],
            trajectory_id=trajectory_id,
            status=execution["status"],
        )
        assert receipt_id is not None
        self._complete_receipt(
            receipt_id,
            response.model_dump(mode="json"),
            entity_type="integration.artifact",
            entity_public_id=artifact["public_id"],
            batch_id=batch_id,
        )
        return response

    def finish_execution(
        self,
        execution_id: UUID,
        payload: FinishExecutionRequest,
        idempotency_key: str,
    ) -> IngestionResult:
        source = self._source()
        execution = self._execution(execution_id)
        digest = self.payload_hash(payload)
        receipt_id, replay = self._claim(
            source_id=source["id"],
            request_kind="EXECUTION_FINISH",
            idempotency_key=idempotency_key,
            producer_key=payload.producer_key,
            payload_hash=digest,
        )
        if replay is not None:
            return IngestionResult.model_validate(replay)
        if execution["status"] not in {"RUNNING", "BLOCKED"}:
            raise ApiError(
                409,
                "execution_state_conflict",
                "只有RUNNING或BLOCKED execution可以finish。",
            )
        if payload.finished_at < execution["normalized_started_at"]:
            raise ApiError(
                422, "time_mismatch", "finished_at早于execution开始时间。"
            )
        wall_seconds = (
            payload.finished_at - execution["normalized_started_at"]
        ).total_seconds()
        if payload.active_seconds > wall_seconds + 0.001:
            raise ApiError(
                422,
                "active_time_invalid",
                "active_seconds不能超过execution墙钟区间。",
            )
        batch_id = self._start_batch(
            source_id=source["id"],
            execution_db_id=execution["id"],
            producer_key=payload.producer_key,
            batch_kind="FINISH",
            payload_hash=digest,
            item_count=1,
        )
        mapped = STATUS_MAP[payload.source_status]
        self.session.execute(
            text(
                """
                UPDATE test.test_execution
                SET status = :status, source_time = :finished_at,
                    received_at = clock_timestamp(),
                    normalized_ended_at = :finished_at,
                    clock_quality = :clock_quality,
                    data_quality = :data_quality
                WHERE id = :execution_id
                """
            ),
            {
                "status": mapped,
                "finished_at": payload.finished_at,
                "clock_quality": payload.clock_quality,
                "data_quality": payload.data_quality,
                "execution_id": execution["id"],
            },
        )
        self.session.execute(
            text(
                """
                UPDATE test.execution_stage
                SET status = :status, source_time = :finished_at,
                    received_at = clock_timestamp(),
                    normalized_ended_at = :finished_at,
                    clock_quality = :clock_quality
                WHERE execution_id = :execution_id
                  AND status IN ('SCHEDULED', 'RUNNING', 'PAUSED')
                """
            ),
            {
                "status": mapped,
                "finished_at": payload.finished_at,
                "clock_quality": payload.clock_quality,
                "execution_id": execution["id"],
            },
        )
        self.session.execute(
            text(
                """
                UPDATE test.execution_result
                SET source_status = :source_status, outcome = :outcome,
                    termination_kind = :termination_kind, summary = :summary,
                    issues = :issues, exception_count = :exception_count,
                    reported_duration_seconds = :active_seconds,
                    last_data_at = :finished_at,
                    source_updated_at = :finished_at
                WHERE execution_id = :execution_id
                """
            ),
            {
                **payload.model_dump(),
                "execution_id": execution["id"],
            },
        )
        runtime_id = self.session.execute(
            text(
                """
                INSERT INTO test.runtime_interval(
                    campaign_id, asset_id, cycle_id, execution_id, source_id,
                    source_kind, source_record_key, source_time, received_at,
                    started_at, ended_at, active_seconds, clock_quality,
                    data_quality, source_method
                ) VALUES (
                    :campaign_id, :asset_id, :cycle_id, :execution_id,
                    :source_id, 'NATIVE', :producer_key, :finished_at,
                    clock_timestamp(), :started_at, :finished_at,
                    :active_seconds, :clock_quality, :data_quality,
                    'edge-collector-reported-active-seconds'
                )
                RETURNING public_id
                """
            ),
            {
                **execution,
                "source_id": source["id"],
                "producer_key": payload.producer_key,
                "finished_at": payload.finished_at,
                "started_at": execution["normalized_started_at"],
                "active_seconds": payload.active_seconds,
                "clock_quality": payload.clock_quality,
                "data_quality": payload.data_quality,
            },
        ).scalar_one()
        event_id = self.session.execute(
            text(
                """
                INSERT INTO test.test_event(
                    campaign_id, asset_id, cycle_id, execution_id,
                    event_type, source_time, normalized_time, clock_quality,
                    data_quality, source_id, source_event_key, payload
                ) VALUES (
                    :campaign_id, :asset_id, :cycle_id, :execution_id,
                    'EXECUTION_FINISH', :finished_at, :finished_at,
                    :clock_quality, :data_quality, :source_id,
                    :source_event_key, CAST(:payload AS jsonb)
                )
                RETURNING public_id
                """
            ),
            {
                **execution,
                "finished_at": payload.finished_at,
                "clock_quality": payload.clock_quality,
                "data_quality": payload.data_quality,
                "source_id": source["id"],
                "source_event_key": f"{payload.producer_key}:finish",
                "payload": json.dumps(
                    {
                        "source_status": payload.source_status,
                        "outcome": payload.outcome,
                        "termination_kind": payload.termination_kind,
                        "active_seconds": payload.active_seconds,
                    },
                    ensure_ascii=False,
                ),
            },
        ).scalar_one()
        self._finish_batch(batch_id, 1)
        response = IngestionResult(
            execution_id=execution_id,
            accepted_count=1,
            event_ids=[event_id],
            runtime_interval_id=runtime_id,
            status=mapped,
            source_status=payload.source_status,
        )
        assert receipt_id is not None
        self._complete_receipt(
            receipt_id,
            response.model_dump(mode="json"),
            entity_type="test.test_execution",
            entity_public_id=execution_id,
            batch_id=batch_id,
        )
        return response
