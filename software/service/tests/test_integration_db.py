from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.db import engine
from app.core.errors import ApiError
from app.repositories.mtbf import MtbfRepository
from app.repositories.read_models import ReadModelRepository
from app.schemas.mtbf import CampaignMtbfConfigRequest
from app.services.worker import claim_job, process_job


pytestmark = pytest.mark.integration


def test_imported_execution_detail_exposes_normalized_result():
    with engine.connect() as connection:
        transaction = connection.begin()
        session = Session(bind=connection, expire_on_commit=False)
        try:
            session.execute(
                text("SELECT iam.set_request_context(:user_id, :request_id, :reason)"),
                {
                    "user_id": "00000000-0000-7000-8000-000000000002",
                    "request_id": "integration-imported-execution-result",
                    "reason": "verify imported execution detail result",
                },
            )
            execution_id = session.execute(
                text(
                    """
                    SELECT execution.public_id
                    FROM integration.execution_import_record imported
                    JOIN test.test_execution execution
                      ON execution.id = imported.execution_id
                    WHERE imported.source_execution_key =
                      'REL-SLEG-010_2607011000_RP1.3-LOWER-01_RD-02'
                    """
                )
            ).scalar_one()

            detail = ReadModelRepository(session).get_execution(str(execution_id))

            assert detail.execution.asset_code == "RP1.3-SLEG-001"
            assert detail.result is not None
            assert detail.result.source_status == "failed"
            assert detail.result.outcome == "FAILED"
            assert detail.result.reported_duration_seconds == 8820
            assert detail.result.issues
            assert detail.result.telemetry_source
            assert detail.result.normalization_flags
            assert any(
                flag["code"] == "case_part_overrode_source_part"
                for flag in detail.result.normalization_flags
            )
            assert detail.evidence
        finally:
            transaction.rollback()
            session.close()


def test_showcase_part_scope_and_performance_trend_contract():
    with engine.connect() as connection:
        transaction = connection.begin()
        session = Session(bind=connection, expire_on_commit=False)
        try:
            session.execute(
                text("SELECT iam.set_request_context(:user_id, :request_id, :reason)"),
                {
                    "user_id": "00000000-0000-7000-8000-000000000002",
                    "request_id": "integration-showcase-trends",
                    "reason": "validate part aggregation and staged trends",
                },
            )
            repository = ReadModelRepository(session)
            dashboard = repository.dashboard_home()
            legs = [
                item
                for group in dashboard.groups
                for item in group.objects
                if item.id == "MODULE:SLEG"
            ]
            assert len(legs) == 1
            assert legs[0].code == "SLEG"
            assert legs[0].part_asset_count == 4
            assert legs[0].part_durations.total_duration_seconds > 4_680_000
            assert "RP1.3-SLEG-014" not in dashboard.model_dump_json()
            assert "RP1.3-SLEG-009" not in dashboard.model_dump_json()
            assert legs[0].part_durations.total_duration_seconds == pytest.approx(
                sum(item.cumulative_duration_seconds for item in legs[0].test_cases)
            )
            whole_scopes = [
                item
                for group in dashboard.groups
                for item in group.objects
                if item.asset_kind == "WHOLE_MACHINE"
            ]
            assert len(whole_scopes) == 1
            assert whole_scopes[0].id == "WHOLE_MACHINE:SYS"
            assert whole_scopes[0].part_asset_count == 4

            for target, expected in (("SLEG", 4), ("HEAD", 1), ("SYS", 4)):
                assets = repository.list_assets(
                    asset_kind="WHOLE_MACHINE" if target == "SYS" else "MODULE",
                    target_part_code=target,
                    status=None,
                    search=None,
                    limit=50,
                    cursor=None,
                )
                assert len(assets.items) == expected
                assert {item.target_part_code for item in assets.items} == {target}

            trend = repository.asset_performance_trends("RP1.3-SLEG-014")
            assert trend.asset_code == "RP1.3-SLEG-014"
            assert len(trend.metrics) >= 3
            for metric in trend.metrics:
                assert [series.sequence_no for series in metric.series] == [1, 2, 3, 4]
                for series in metric.series:
                    assert len(series.points) >= 10
                    assert [point.observed_at for point in series.points] == sorted(
                        point.observed_at for point in series.points
                    )
            battery_trend = repository.asset_performance_trends("RP1.3-BAT-018")
            assert len(battery_trend.metrics) >= 3
            assert all(
                [series.sequence_no for series in metric.series] == [1, 2, 3, 4]
                for metric in battery_trend.metrics
            )
            with pytest.raises(ApiError) as missing:
                repository.asset_performance_trends("RP999.9-SLEG-999")
            assert missing.value.status_code == 404
        finally:
            transaction.rollback()
            session.close()


def test_campaign_config_queue_worker_and_projection_round_trip():
    suffix = uuid4().hex[:10]
    asset_version = int(suffix, 16)
    with engine.connect() as connection:
        transaction = connection.begin()
        session = Session(bind=connection, expire_on_commit=False)
        try:
            session.execute(
                text("SELECT iam.set_request_context(:user_id, :request_id, :reason)"),
                {
                    "user_id": "00000000-0000-7000-8000-000000000002",
                    "request_id": f"integration-{suffix}",
                    "reason": "automated integration test",
                },
            )
            actor_id = session.execute(text("SELECT iam.current_user_id()" )).scalar_one()
            program_id = session.execute(
                text(
                    """
                    INSERT INTO test.test_program(program_code, name, status, owner_id)
                    VALUES (:code, '集成测试项目', 'ACTIVE', :actor_id)
                    RETURNING id
                    """
                ),
                {"code": f"IT-P-{suffix}", "actor_id": actor_id},
            ).scalar_one()
            campaign = session.execute(
                text(
                    """
                    INSERT INTO test.test_campaign(
                        program_id, campaign_code, name, asset_kind, status
                    ) VALUES (:program_id, :code, 'Campaign MTBF集成测试', 'WHOLE_MACHINE', 'ACTIVE')
                    RETURNING id, public_id
                    """
                ),
                {"program_id": program_id, "code": f"IT-C-{suffix}"},
            ).mappings().one()
            asset = session.execute(
                text(
                    """
                    INSERT INTO test.asset(
                        asset_code, asset_kind, product_family, serial_number,
                        lifecycle_status, display_name
                    ) VALUES (:code, 'WHOLE_MACHINE', 'RP1', :serial, 'ACTIVE', '集成测试样品')
                    RETURNING id, public_id
                    """
                ),
                {
                    "code": f"RP{asset_version}.1-SYS-001",
                    "serial": f"IT-SN-{suffix}",
                },
            ).mappings().one()
            session.execute(
                text(
                    """
                    INSERT INTO test.campaign_asset(campaign_id, asset_id, participation_role)
                    VALUES (:campaign_id, :asset_id, 'PRIMARY')
                    """
                ),
                {"campaign_id": campaign["id"], "asset_id": asset["id"]},
            )
            scope_id = session.execute(
                text(
                    """
                    SELECT id FROM reliability.mtbf_scope
                    WHERE scope_code = 'OVERALL' AND asset_kind = 'WHOLE_MACHINE'
                      AND status = 'PUBLISHED'
                    ORDER BY version DESC LIMIT 1
                    """
                )
            ).scalar_one()
            started = datetime.now(timezone.utc) - timedelta(hours=3)
            interval_id = session.execute(
                text(
                    """
                    INSERT INTO test.runtime_interval(
                        campaign_id, asset_id, source_kind, started_at, ended_at,
                        active_seconds, clock_quality, data_quality, source_method
                    ) VALUES (
                        :campaign_id, :asset_id, 'NATIVE', :started_at, :ended_at,
                        7200, 'VALID', 'VALID', 'integration-test'
                    ) RETURNING id
                    """
                ),
                {
                    "campaign_id": campaign["id"],
                    "asset_id": asset["id"],
                    "started_at": started,
                    "ended_at": started + timedelta(hours=2),
                },
            ).scalar_one()
            assessment_id = session.execute(
                text(
                    """
                    INSERT INTO reliability.exposure_assessment(
                        campaign_id, asset_id, runtime_interval_id, eligible,
                        assignment_status, quality_status, assessment_method
                    ) VALUES (
                        :campaign_id, :asset_id, :interval_id, true,
                        'CONFIRMED', 'VALID', 'integration-test'
                    ) RETURNING id
                    """
                ),
                {
                    "campaign_id": campaign["id"],
                    "asset_id": asset["id"],
                    "interval_id": interval_id,
                },
            ).scalar_one()
            session.execute(
                text(
                    """
                    INSERT INTO reliability.exposure_scope_assignment(
                        campaign_id, assessment_id, scope_id, inclusion_status, rationale
                    ) VALUES (
                        :campaign_id, :assessment_id, :scope_id, 'INCLUDED', '测试用例有效任务'
                    )
                    """
                ),
                {
                    "campaign_id": campaign["id"],
                    "assessment_id": assessment_id,
                    "scope_id": scope_id,
                },
            )
            config = MtbfRepository(session).upsert_config(
                campaign["public_id"],
                "OVERALL",
                CampaignMtbfConfigRequest(
                    target_hours=1000,
                    confidence_levels=[0.70, 0.90],
                    settings={"source": "integration-test"},
                ),
                None,
            )
            assert config.target_hours == 1000
            assert config.method == "mtbf.poisson.exposure_estimate.v1"
            repeated = MtbfRepository(session).upsert_config(
                campaign["public_id"],
                "OVERALL",
                CampaignMtbfConfigRequest(
                    target_hours=1000,
                    confidence_levels=[0.70, 0.90],
                    settings={"source": "integration-test"},
                ),
                None,
            )
            assert repeated.id == config.id

            job = claim_job(session, f"integration-worker-{suffix}")
            assert job is not None
            process_job(session, job)
            projection = session.execute(
                text(
                    """
                    SELECT exposure_seconds, relevant_failure_count,
                           point_estimate_hours, lower_70_hours, result_payload
                    FROM reliability.current_mtbf_result
                    WHERE campaign_id = :campaign_id AND scope_id = :scope_id
                    """
                ),
                {"campaign_id": campaign["id"], "scope_id": scope_id},
            ).mappings().one()
            assert float(projection["exposure_seconds"]) == 7200
            assert projection["relevant_failure_count"] == 0
            assert projection["point_estimate_hours"] is None
            assert float(projection["lower_70_hours"]) > 0
            assert projection["result_payload"]["per_asset_exposure"][0]["included_hours"] == 2
        finally:
            session.close()
            transaction.rollback()


def test_same_minute_executions_receive_distinct_codes():
    with engine.connect() as connection:
        transaction = connection.begin()
        session = Session(bind=connection, expire_on_commit=False)
        try:
            session.execute(
                text("SELECT iam.set_request_context(:user_id, :request_id, :reason)"),
                {
                    "user_id": "00000000-0000-7000-8000-000000000002",
                    "request_id": "integration-execution-code-collision",
                    "reason": "verify same-minute execution identifiers",
                },
            )
            context = session.execute(
                text(
                    """
                    SELECT campaign_id, cycle_id, asset_id,
                           configuration_snapshot_id, test_case_version_id,
                           station_id, ingestion_segment_id
                    FROM test.test_execution
                    ORDER BY id
                    LIMIT 1
                    """
                )
            ).mappings().one()
            code_timestamp = datetime.now(timezone.utc).replace(
                second=0, microsecond=0
            )

            codes: list[tuple[int, str]] = []
            for _ in range(2):
                row = session.execute(
                    text(
                        """
                        INSERT INTO test.test_execution(
                            execution_code, campaign_id, cycle_id, asset_id,
                            configuration_snapshot_id, test_case_version_id,
                            station_id, ingestion_segment_id, status,
                            source_time, received_at, normalized_started_at,
                            code_timestamp, clock_quality, data_quality
                        ) VALUES (
                            'GENERATED', :campaign_id, :cycle_id, :asset_id,
                            :configuration_snapshot_id, :test_case_version_id,
                            :station_id, :ingestion_segment_id, 'SCHEDULED',
                            :started_at, :started_at, :started_at,
                            :started_at, 'VALID', 'VALID'
                        )
                        RETURNING id, execution_code
                        """
                    ),
                    {**dict(context), "started_at": code_timestamp},
                ).one()
                codes.append((row.id, row.execution_code))

            assert codes[0][1] != codes[1][1]
            assert codes[0][1].endswith(f"_E{codes[0][0]}")
            assert codes[1][1].endswith(f"_E{codes[1][0]}")
        finally:
            transaction.rollback()
            session.close()


def test_read_model_vertical_slice_isolated_durations_drilldown_and_rls():
    suffix = uuid4().hex[:10]
    asset_version = int(suffix, 16)
    with engine.connect() as connection:
        transaction = connection.begin()
        session = Session(bind=connection, expire_on_commit=False)
        try:
            session.execute(
                text("SELECT iam.set_request_context(:user_id, :request_id, :reason)"),
                {
                    "user_id": "00000000-0000-7000-8000-000000000002",
                    "request_id": f"read-model-{suffix}",
                    "reason": "read model integration test",
                },
            )
            actor_id = session.execute(text("SELECT iam.current_user_id()")).scalar_one()
            program_id = session.execute(
                text(
                    """
                    INSERT INTO test.test_program(program_code, name, status, owner_id)
                    VALUES (:code, 'Read model integration', 'ACTIVE', :actor_id)
                    RETURNING id
                    """
                ),
                {"code": f"RM-P-{suffix}", "actor_id": actor_id},
            ).scalar_one()
            site_id = session.execute(
                text(
                    """
                    INSERT INTO test.site(site_code, name, location)
                    VALUES (:code, 'Read model site', 'Integration test')
                    RETURNING id
                    """
                ),
                {"code": f"RM-SITE-{suffix}"},
            ).scalar_one()
            lab_id = session.execute(
                text(
                    """
                    INSERT INTO test.lab(site_id, lab_code, name)
                    VALUES (:site_id, :code, 'Read model lab')
                    RETURNING id
                    """
                ),
                {"site_id": site_id, "code": f"RM-LAB-{suffix}"},
            ).scalar_one()
            created: dict[str, dict] = {}
            for kind, prefix in (("WHOLE_MACHINE", "WHOLE"), ("MODULE", "MODULE")):
                station_id = session.execute(
                    text(
                        """
                        INSERT INTO test.station(
                            lab_id, station_code, name, station_type
                        ) VALUES (:lab_id, :code, :name, :kind)
                        RETURNING id
                        """
                    ),
                    {
                        "lab_id": lab_id,
                        "code": f"RM-{prefix}-{suffix}",
                        "name": f"{prefix} station",
                        "kind": kind,
                    },
                ).scalar_one()
                campaign = session.execute(
                    text(
                        """
                        INSERT INTO test.test_campaign(
                            program_id, campaign_code, name, asset_kind, status
                        ) VALUES (:program_id, :code, :name, :kind, 'ACTIVE')
                        RETURNING id, public_id
                        """
                    ),
                    {
                        "program_id": program_id,
                        "code": f"RM-C-{prefix}-{suffix}",
                        "name": f"{prefix} campaign",
                        "kind": kind,
                    },
                ).mappings().one()
                asset = session.execute(
                    text(
                        """
                        INSERT INTO test.asset(
                            asset_code, asset_kind, product_family, serial_number,
                            lifecycle_status, display_name
                        ) VALUES (:code, :kind, 'RP1', :serial, 'ACTIVE', :name)
                        RETURNING id, public_id
                        """
                    ),
                    {
                        "code": (
                            f"RP{asset_version}.1-"
                            f"{'SYS' if kind == 'WHOLE_MACHINE' else 'SLEG'}-001"
                        ),
                        "kind": kind,
                        "serial": f"RM-SN-{prefix}-{suffix}",
                        "name": f"{prefix} asset",
                    },
                ).mappings().one()
                session.execute(
                    text(
                        """
                        INSERT INTO test.campaign_asset(campaign_id, asset_id, participation_role)
                        VALUES (:campaign_id, :asset_id, 'PRIMARY')
                        """
                    ),
                    {"campaign_id": campaign["id"], "asset_id": asset["id"]},
                )
                target_part_code = "SYS" if kind == "WHOLE_MACHINE" else "SLEG"
                if kind == "MODULE":
                    session.execute(
                        text(
                            """
                            INSERT INTO test.module_profile(
                                asset_id, module_type, target_part_code
                            ) VALUES (:asset_id, 'TEST_LEG', :target_part_code)
                            """
                        ),
                        {
                            "asset_id": asset["id"],
                            "target_part_code": target_part_code,
                        },
                    )
                case = session.execute(
                    text(
                        """
                        INSERT INTO catalog.test_case(
                            case_code, name, asset_kind, target_part_code,
                            domain, evidence_type
                        ) VALUES (
                            :code, :name, :kind, :target_part_code,
                            'AGING', 'RUNTIME'
                        )
                        RETURNING id, public_id
                        """
                    ),
                    {
                        "code": f"RM-TC-{prefix}-{suffix}",
                        "name": f"{prefix} aging",
                        "kind": kind,
                        "target_part_code": target_part_code,
                    },
                ).mappings().one()
                version_id = session.execute(
                    text(
                        """
                        INSERT INTO catalog.test_case_version(
                            test_case_id, version, procedure_spec, stage_definitions, status
                        ) VALUES (
                            :case_id, '1.0',
                            CAST(:procedure_spec AS jsonb),
                            '[]'::jsonb, 'PUBLISHED'
                        ) RETURNING id
                        """
                    ),
                    {
                        "case_id": case["id"],
                        "procedure_spec": json.dumps({"target_duration_seconds": 7200}),
                    },
                ).scalar_one()
                not_started_case_id = session.execute(
                    text(
                        """
                        INSERT INTO catalog.test_case(
                            case_code, name, asset_kind, target_part_code,
                            domain, evidence_type
                        ) VALUES (
                            :code, :name, :kind, :target_part_code,
                            'AGING', 'RUNTIME'
                        ) RETURNING id
                        """
                    ),
                    {
                        "code": f"RM-TC-{prefix}-NOT-STARTED-{suffix}",
                        "name": f"{prefix} not started",
                        "kind": kind,
                        "target_part_code": target_part_code,
                    },
                ).scalar_one()
                session.execute(
                    text(
                        """
                        INSERT INTO catalog.test_case_version(
                            test_case_id, version, procedure_spec,
                            stage_definitions, status
                        ) VALUES (
                            :case_id, '1.0', '{}'::jsonb, '[]'::jsonb, 'PUBLISHED'
                        )
                        """
                    ),
                    {"case_id": not_started_case_id},
                )
                configuration_id = session.execute(
                    text(
                        """
                        INSERT INTO test.configuration_snapshot(
                            asset_id, fingerprint, captured_from, effective_from
                        ) VALUES (:asset_id, :fingerprint, 'integration-test', :effective_from)
                        RETURNING id
                        """
                    ),
                    {
                        "asset_id": asset["id"],
                        "fingerprint": f"RM-FP-{prefix}-{suffix}",
                        "effective_from": datetime.now(timezone.utc) - timedelta(days=1),
                    },
                ).scalar_one()
                cycle_id = session.execute(
                    text(
                        """
                        INSERT INTO test.test_cycle(
                            cycle_code, campaign_id, asset_id, status, started_at
                        ) VALUES (:code, :campaign_id, :asset_id, 'ACTIVE', :started_at)
                        RETURNING id
                        """
                    ),
                    {
                        "code": f"RM-CY-{prefix}-{suffix}",
                        "campaign_id": campaign["id"],
                        "asset_id": asset["id"],
                        "started_at": datetime.now(timezone.utc) - timedelta(hours=3),
                    },
                ).scalar_one()
                started = datetime.now(timezone.utc) - timedelta(hours=2)
                execution = session.execute(
                    text(
                        """
                        INSERT INTO test.test_execution(
                            execution_code, campaign_id, cycle_id, asset_id,
                            configuration_snapshot_id, test_case_version_id, station_id,
                            status, normalized_started_at, normalized_ended_at,
                            received_at, code_timestamp, data_quality, clock_quality
                        ) VALUES (
                            :code, :campaign_id, :cycle_id, :asset_id,
                            :configuration_id, :version_id, :station_id, 'COMPLETED',
                            :started_at, :ended_at, :ended_at, :started_at,
                            'VALID', 'VALID'
                        ) RETURNING id, public_id
                        """
                    ),
                    {
                        "code": f"RM-EX-{prefix}-{suffix}",
                        "campaign_id": campaign["id"],
                        "cycle_id": cycle_id,
                        "asset_id": asset["id"],
                        "configuration_id": configuration_id,
                        "version_id": version_id,
                        "station_id": station_id,
                        "started_at": started,
                        "ended_at": started + timedelta(hours=1),
                    },
                ).mappings().one()
                interval_id = session.execute(
                    text(
                        """
                        INSERT INTO test.runtime_interval(
                            campaign_id, asset_id, cycle_id, execution_id,
                            source_kind, started_at, ended_at, active_seconds,
                            clock_quality, data_quality, source_method
                        ) VALUES (
                            :campaign_id, :asset_id, :cycle_id, :execution_id,
                            'NATIVE', :started_at, :ended_at, 3600,
                            'VALID', 'VALID', 'integration-test'
                        ) RETURNING id
                        """
                    ),
                    {
                        "campaign_id": campaign["id"],
                        "asset_id": asset["id"],
                        "cycle_id": cycle_id,
                        "execution_id": execution["id"],
                        "started_at": started,
                        "ended_at": started + timedelta(hours=1),
                    },
                ).scalar_one()
                artifact = session.execute(
                    text(
                        """
                        INSERT INTO integration.artifact(
                            campaign_id, asset_id, cycle_id, execution_id, kind,
                            availability_status, object_key, source_location,
                            file_name, mime_type, size_bytes
                        ) VALUES (
                            :campaign_id, :asset_id, :cycle_id, :execution_id,
                            'RAW_LOG_BUNDLE', 'AVAILABLE', :object_key,
                            'integration-test', :file_name, 'application/zip', 1024
                        ) RETURNING public_id
                        """
                    ),
                    {
                        "campaign_id": campaign["id"],
                        "asset_id": asset["id"],
                        "cycle_id": cycle_id,
                        "execution_id": execution["id"],
                        "object_key": f"integration/{suffix}/{prefix}.zip",
                        "file_name": f"{prefix.lower()}-evidence.zip",
                    },
                ).scalar_one()
                assessment_id = session.execute(
                    text(
                        """
                        INSERT INTO reliability.exposure_assessment(
                            campaign_id, asset_id, runtime_interval_id,
                            configuration_snapshot_id, eligible,
                            assignment_status, quality_status, assessment_method
                        ) VALUES (
                            :campaign_id, :asset_id, :interval_id,
                            :configuration_id, true, 'CONFIRMED', 'VALID',
                            'integration-test'
                        ) RETURNING id
                        """
                    ),
                    {
                        "campaign_id": campaign["id"],
                        "asset_id": asset["id"],
                        "interval_id": interval_id,
                        "configuration_id": configuration_id,
                    },
                ).scalar_one()
                scope_id = session.execute(
                    text(
                        """
                        SELECT id FROM reliability.mtbf_scope
                        WHERE scope_code = 'OVERALL' AND asset_kind = :kind
                          AND status = 'PUBLISHED'
                        ORDER BY version DESC LIMIT 1
                        """
                    ),
                    {"kind": kind},
                ).scalar_one()
                session.execute(
                    text(
                        """
                        INSERT INTO reliability.exposure_scope_assignment(
                            campaign_id, assessment_id, scope_id,
                            inclusion_status, rationale
                        ) VALUES (
                            :campaign_id, :assessment_id, :scope_id,
                            'INCLUDED', 'integration-test'
                        )
                        """
                    ),
                    {
                        "campaign_id": campaign["id"],
                        "assessment_id": assessment_id,
                        "scope_id": scope_id,
                    },
                )
                created[kind] = {
                    "asset": asset,
                    "case": case,
                    "execution": execution,
                    "artifact": artifact,
                }

            repository = ReadModelRepository(session)
            dashboard = repository.dashboard_home()
            dashboard_objects = [
                item
                for group in dashboard.groups
                for item in group.objects
                if item.id in {"WHOLE_MACHINE:SYS", "MODULE:SLEG"}
            ]
            assert len(dashboard_objects) == 2
            for item in dashboard_objects:
                expected_target = (
                    "SYS" if item.asset_kind == "WHOLE_MACHINE" else "SLEG"
                )
                assert item.target_part_code == expected_target
                scoped_cases = [case for case in item.test_cases if suffix in case.code]
                assert len(scoped_cases) == 2
                assert {case.target_part_code for case in scoped_cases} == {expected_target}
                assert {case.status for case in scoped_cases} == {"COMPLETED", "NOT_STARTED"}

            whole = repository.test_case_durations(
                asset_kind="WHOLE_MACHINE",
                asset=None,
                test_case=None,
                status=None,
                search=suffix,
                limit=50,
                cursor=None,
            )
            assert len(whole.items) == 1
            assert whole.items[0].asset_kind == "WHOLE_MACHINE"
            assert whole.items[0].durations.total_duration_seconds == 3600
            assert whole.items[0].durations.effective_exposure_seconds == 3600

            module = repository.test_case_durations(
                asset_kind="MODULE",
                asset=None,
                test_case=None,
                status=None,
                search=suffix,
                limit=50,
                cursor=None,
            )
            assert len(module.items) == 1
            assert module.items[0].asset_kind == "MODULE"

            empty = repository.test_case_durations(
                asset_kind="WHOLE_MACHINE",
                asset=None,
                test_case=None,
                status=None,
                search=f"missing-{suffix}",
                limit=50,
                cursor=None,
            )
            assert empty.items == []
            assert empty.totals.total_duration_seconds == 0

            first_page = repository.list_assets(
                asset_kind=None, target_part_code=None, status=None, search=suffix, limit=1, cursor=None
            )
            assert len(first_page.items) == 1
            assert first_page.page.next_cursor is not None

            whole_asset = created["WHOLE_MACHINE"]["asset"]
            asset_detail = repository.get_asset(str(whole_asset["public_id"]))
            assert asset_detail.execution_count == 1
            assert asset_detail.asset.durations.effective_exposure_seconds == 3600
            assert repository.asset_executions(
                str(whole_asset["public_id"]), 50, None
            ).items[0].closed_duration_seconds == 3600
            execution_detail = repository.get_execution(
                str(created["WHOLE_MACHINE"]["execution"]["public_id"])
            )
            assert execution_detail.metrics == []
            assert len(execution_detail.evidence) == 1
            assert execution_detail.evidence[0].id == created["WHOLE_MACHINE"]["artifact"]
            case_detail = repository.get_test_case(
                str(created["WHOLE_MACHINE"]["case"]["public_id"]),
                limit=50,
                cursor=None,
            )
            assert case_detail.mtbf_observation is not None
            assert case_detail.mtbf_observation.relevant_failure_count == 0
            assert case_detail.mtbf_observation.point_estimate_hours is None

            viewer_public_id = uuid4()
            session.execute(
                text(
                    """
                    INSERT INTO iam.app_user(
                        public_id, username, display_name, role,
                        principal_kind, enabled, must_change_password
                    ) VALUES (
                        :public_id, :username, 'Read model viewer', 'VIEWER',
                        'HUMAN', true, false
                    )
                    """
                ),
                {"public_id": str(viewer_public_id), "username": f"rm-viewer-{suffix}"},
            )
            session.execute(
                text("SELECT iam.set_request_context(:user_id, :request_id, :reason)"),
                {
                    "user_id": str(viewer_public_id),
                    "request_id": f"read-model-viewer-{suffix}",
                    "reason": "",
                },
            )
            hidden = repository.list_assets(
                asset_kind=None, target_part_code=None, status=None, search=suffix, limit=50, cursor=None
            )
            assert hidden.items == []
        finally:
            session.close()
            transaction.rollback()
