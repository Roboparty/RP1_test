\set ON_ERROR_STOP on

BEGIN;

DO $showcase$
DECLARE
    asset_row record;
    case_row record;
    kind_row record;
    metric_row record;
    stage_row record;
    v_actor bigint;
    v_source bigint;
    v_program bigint;
    v_site bigint;
    v_lab bigint;
    v_batch bigint;
    v_campaign bigint;
    v_station bigint;
    v_asset bigint;
    v_config bigint;
    v_cycle bigint;
    v_segment bigint;
    v_policy bigint;
    v_case bigint;
    v_version bigint;
    v_execution bigint;
    v_stage bigint;
    v_interval bigint;
    v_assessment bigint;
    v_event bigint;
    v_interruption bigint;
    v_scope bigint;
    v_error_version bigint;
    v_error_code bigint;
    v_metric_version bigint;
    v_population bigint;
    v_method bigint;
    v_run bigint;
    v_health bigint;
    v_status text;
    v_health_status text;
    v_started timestamptz;
    v_ended timestamptz;
    v_received timestamptz;
    v_seconds numeric;
    v_asset_index integer;
BEGIN
    SELECT id INTO v_actor
    FROM iam.app_user
    WHERE public_id = '00000000-0000-7000-8000-000000000002'::uuid;

    PERFORM iam.set_request_context(
        '00000000-0000-7000-8000-000000000002'::uuid,
        'showcase-seed',
        'manual development showcase seed'
    );

    INSERT INTO integration.ingestion_source(
        source_code, source_type, direction, status, config_reference
    ) VALUES (
        'SHOWCASE-COLLECTOR', 'SYSTEM', 'INTERNAL', 'ACTIVE',
        'development-only showcase seed'
    )
    ON CONFLICT (source_code) DO UPDATE SET
        status = 'ACTIVE',
        config_reference = EXCLUDED.config_reference,
        updated_at = clock_timestamp()
    RETURNING id INTO v_source;

    INSERT INTO integration.ingestion_receipt(
        source_id, idempotency_key, source_time, received_at, payload_hash,
        entity_type, status
    ) VALUES (
        v_source, 'SHOWCASE-SEED-RECEIPT', clock_timestamp(), clock_timestamp(),
        'showcase-seed-v1', 'SHOWCASE_DATASET', 'ACCEPTED'
    )
    ON CONFLICT (source_id, idempotency_key) DO UPDATE SET
        source_time = EXCLUDED.source_time,
        received_at = EXCLUDED.received_at,
        payload_hash = EXCLUDED.payload_hash,
        status = 'ACCEPTED',
        error_detail = '';

    INSERT INTO test.test_program(program_code, name, objective, status, owner_id)
    VALUES (
        'SHOWCASE-RP1-PROGRAM',
        'RP1 可靠性 Showcase',
        '仅用于开发环境视觉验收的隔离示例数据',
        'ACTIVE',
        v_actor
    )
    ON CONFLICT (program_code) DO UPDATE SET
        name = EXCLUDED.name,
        objective = EXCLUDED.objective,
        status = 'ACTIVE',
        updated_at = clock_timestamp()
    RETURNING id INTO v_program;

    INSERT INTO test.site(site_code, name, location, enabled)
    VALUES ('SHOWCASE-SITE', 'Showcase 可靠性实验场', 'Development only', true)
    ON CONFLICT (site_code) DO UPDATE SET
        name = EXCLUDED.name,
        enabled = true,
        updated_at = clock_timestamp()
    RETURNING id INTO v_site;

    INSERT INTO test.lab(site_id, lab_code, name, enabled)
    VALUES (v_site, 'SHOWCASE-LAB', 'Showcase 综合可靠性实验室', true)
    ON CONFLICT (site_id, lab_code) DO UPDATE SET
        name = EXCLUDED.name,
        enabled = true,
        updated_at = clock_timestamp()
    RETURNING id INTO v_lab;

    INSERT INTO catalog.error_catalog_version(version, status, published_by, published_at)
    VALUES ('SHOWCASE-1.0', 'PUBLISHED', v_actor, clock_timestamp())
    ON CONFLICT (version) DO UPDATE SET status = 'PUBLISHED'
    RETURNING id INTO v_error_version;

    INSERT INTO catalog.error_code(
        catalog_version_id, unified_code, domain, subsystem, category, title,
        description, default_classification
    ) VALUES (
        v_error_version, 'SHOWCASE-E-DRIVE-042', 'HARDWARE', 'DRIVE',
        'OVER_TEMPERATURE', '驱动温升超限',
        'Showcase 故障事实，仅用于开发视觉验收', 'FAILED'
    )
    ON CONFLICT (catalog_version_id, unified_code) DO UPDATE SET
        title = EXCLUDED.title,
        description = EXCLUDED.description
    RETURNING id INTO v_error_code;

    FOR metric_row IN
        SELECT * FROM (VALUES
            ('SHOWCASE-JOINT-TEMP', '驱动温度', '°C', 'LOWER_BETTER'),
            ('SHOWCASE-JOINT-CURRENT', '驱动电流', 'A', 'LOWER_BETTER'),
            ('SHOWCASE-VIBRATION-RMS', '振动 RMS', 'mm/s', 'LOWER_BETTER')
        ) AS metrics(code, name, unit, direction)
    LOOP
        INSERT INTO catalog.metric_definition(metric_code, canonical_name, domain, enabled)
        VALUES (metric_row.code, metric_row.name, 'SHOWCASE_RELIABILITY', true)
        ON CONFLICT (metric_code) DO UPDATE SET
            canonical_name = EXCLUDED.canonical_name,
            enabled = true,
            updated_at = clock_timestamp()
        RETURNING id INTO v_case;

        INSERT INTO catalog.metric_version(
            metric_definition_id, version, canonical_unit, value_type,
            calculation_key, direction, formal_eligible, status
        ) VALUES (
            v_case, 'SHOWCASE-1.0', metric_row.unit, 'NUMBER',
            'showcase.direct_observation.v1', metric_row.direction, true, 'PUBLISHED'
        )
        ON CONFLICT (metric_definition_id, version) DO UPDATE SET
            canonical_unit = EXCLUDED.canonical_unit,
            formal_eligible = true,
            status = 'PUBLISHED',
            updated_at = clock_timestamp();
    END LOOP;

    -- HEAD and BAT are valid normalized target parts but the imported CSV
    -- catalog has no cases for them yet. These development-only cases let all
    -- twelve showcase assets own one representative execution without
    -- changing the 60-row formal CSV catalog.
    FOR case_row IN
        SELECT *
        FROM (
            VALUES
                (
                    'SHOWCASE-TELEMETRY-HEAD',
                    '头部稳态遥测循环',
                    'HEAD'
                ),
                (
                    'SHOWCASE-TELEMETRY-BAT',
                    '电池稳态遥测循环',
                    'BAT'
                )
        ) AS showcase_cases(case_code, name, target_part_code)
    LOOP
        INSERT INTO catalog.test_case(
            case_code,
            name,
            asset_kind,
            target_part_code,
            domain,
            evidence_type,
            enabled
        )
        VALUES (
            case_row.case_code,
            case_row.name,
            'MODULE',
            case_row.target_part_code,
            'SHOWCASE_RELIABILITY',
            'TELEMETRY',
            true
        )
        ON CONFLICT (case_code) DO UPDATE SET
            name = EXCLUDED.name,
            target_part_code = EXCLUDED.target_part_code,
            enabled = true,
            updated_at = clock_timestamp()
        RETURNING id INTO v_case;

        INSERT INTO catalog.test_case_version(
            test_case_id,
            version,
            procedure_spec,
            stage_definitions,
            metric_requirements,
            sampling_requirements,
            status,
            published_by,
            published_at
        )
        VALUES (
            v_case,
            'SHOWCASE-1.0',
            '{"seed":"SHOWCASE","procedure":"steady telemetry cycle"}'::jsonb,
            '[
                {"code":"PRECHECK","sequence":1},
                {"code":"RAMP_LOAD","sequence":2},
                {"code":"STEADY_RUN","sequence":3},
                {"code":"RECOVERY","sequence":4}
            ]'::jsonb,
            '[
                "JOINT-TARGET-POSITION",
                "JOINT-ACTUAL-POSITION",
                "JOINT-TRACKING-ERROR",
                "JOINT-VELOCITY",
                "JOINT-TORQUE",
                "JOINT-CURRENT",
                "JOINT-TEMPERATURE",
                "JOINT-VIBRATION-RMS"
            ]'::jsonb,
            '{"cycles":3,"downsamplePointsPerSeries":100}'::jsonb,
            'PUBLISHED',
            v_actor,
            timestamptz '2026-09-18 09:00:00+08'
        )
        ON CONFLICT (test_case_id, version) DO UPDATE SET
            procedure_spec = EXCLUDED.procedure_spec,
            stage_definitions = EXCLUDED.stage_definitions,
            metric_requirements = EXCLUDED.metric_requirements,
            sampling_requirements = EXCLUDED.sampling_requirements,
            status = 'PUBLISHED',
            published_by = EXCLUDED.published_by,
            published_at = EXCLUDED.published_at,
            updated_at = clock_timestamp();
    END LOOP;

    FOR kind_row IN
        SELECT * FROM (VALUES
            ('WHOLE_MACHINE', 'SHOWCASE-WHOLE-BATCH', 'SHOWCASE-WHOLE-CAMPAIGN',
             '整机耐久与任务循环', 'SHOWCASE-WHOLE-BENCH', '整机综合环境台',
             'SHOWCASE-WHOLE-POP', '整机 Showcase 统计总体',
             'SHOWCASE-WHOLE-HEALTH', '整机 F1 健康检查'),
            ('MODULE', 'SHOWCASE-MODULE-BATCH', 'SHOWCASE-MODULE-CAMPAIGN',
             '模块关节与执行器耐久', 'SHOWCASE-MODULE-BENCH', '模块循环台',
             'SHOWCASE-MODULE-POP', '模块 Showcase 统计总体',
             'SHOWCASE-MODULE-HEALTH', '模块 F1 健康检查')
        ) AS kinds(
            kind, batch_code, campaign_code, campaign_name, station_code,
            station_name, population_code, population_name, policy_code, policy_name
        )
    LOOP
        INSERT INTO test.manufacturing_batch(
            batch_code, asset_kind, product_family, manufactured_at, notes
        ) VALUES (
            kind_row.batch_code, kind_row.kind, 'RP1-SHOWCASE',
            current_date - 120, 'SHOWCASE development data'
        )
        ON CONFLICT (batch_code) DO UPDATE SET
            notes = EXCLUDED.notes,
            updated_at = clock_timestamp()
        RETURNING id INTO v_batch;

        INSERT INTO test.station(lab_id, station_code, name, station_type, enabled)
        VALUES (
            v_lab, kind_row.station_code, kind_row.station_name, kind_row.kind, true
        )
        ON CONFLICT (station_code) DO UPDATE SET
            name = EXCLUDED.name,
            enabled = true,
            updated_at = clock_timestamp()
        RETURNING id INTO v_station;

        INSERT INTO test.test_campaign(
            program_id, site_id, campaign_code, name, asset_kind, status,
            planned_start, planned_end
        ) VALUES (
            v_program, v_site, kind_row.campaign_code, kind_row.campaign_name,
            kind_row.kind, 'ACTIVE', clock_timestamp() - interval '90 days',
            clock_timestamp() + interval '60 days'
        )
        ON CONFLICT (program_id, campaign_code) DO UPDATE SET
            name = EXCLUDED.name,
            status = 'ACTIVE',
            planned_start = EXCLUDED.planned_start,
            planned_end = EXCLUDED.planned_end,
            updated_at = clock_timestamp()
        RETURNING id INTO v_campaign;

        SELECT id INTO v_scope
        FROM reliability.mtbf_scope
        WHERE scope_code = 'OVERALL'
          AND asset_kind = kind_row.kind
          AND status = 'PUBLISHED'
        ORDER BY version DESC
        LIMIT 1;

        INSERT INTO reliability.mtbf_population(
            population_code, name, asset_kind, status
        ) VALUES (
            kind_row.population_code, kind_row.population_name, kind_row.kind, 'ACTIVE'
        )
        ON CONFLICT (population_code) DO UPDATE SET
            name = EXCLUDED.name,
            status = 'ACTIVE',
            updated_at = clock_timestamp()
        RETURNING id INTO v_population;

        INSERT INTO reliability.population_campaign(population_id, campaign_id)
        VALUES (v_population, v_campaign)
        ON CONFLICT DO NOTHING;

        INSERT INTO catalog.health_check_policy(
            policy_code, version, asset_kind, check_level, trigger_mode,
            method_spec, status, published_by, published_at
        ) VALUES (
            kind_row.policy_code, 'SHOWCASE-1.0', kind_row.kind, 'F1', 'ANY',
            jsonb_build_object('seed', 'SHOWCASE', 'method', 'visual-review'),
            'PUBLISHED', v_actor, clock_timestamp()
        )
        ON CONFLICT (policy_code, version) DO UPDATE SET
            status = 'PUBLISHED',
            method_spec = EXCLUDED.method_spec,
            updated_at = clock_timestamp()
        RETURNING id INTO v_policy;

        v_asset_index := 0;
        FOR asset_row IN
            SELECT * FROM (VALUES
                ('WHOLE_MACHINE', 'RP1.3-SYS-023', 'RP1.3-023 上身集成', 'ACTIVE', 'SC-W-023', 'RP1A', NULL, 'SYS'),
                ('WHOLE_MACHINE', 'RP1.3-SYS-031', 'RP1.3-031 环境复合', 'ACTIVE', 'SC-W-031', 'RP1A', NULL, 'SYS'),
                ('WHOLE_MACHINE', 'RP1.3-SYS-038', 'RP1.3-038 数据补采', 'PAUSED', 'SC-W-038', 'RP1B', NULL, 'SYS'),
                ('WHOLE_MACHINE', 'RP1.3-SYS-044', 'RP1.3-044 检修样机', 'MAINTENANCE', 'SC-W-044', 'RP1B', NULL, 'SYS'),
                ('MODULE', 'RP1.3-SLEG-014', '左膝关节总成', 'ACTIVE', 'SC-M-014', NULL, 'KNEE_JOINT', 'SLEG'),
                ('MODULE', 'RP1.3-SLEG-009', '髋关节总成', 'ACTIVE', 'SC-M-009', NULL, 'HIP_JOINT', 'SLEG'),
                ('MODULE', 'RP1.3-SARM-022', '左灵巧手', 'ACTIVE', 'SC-M-022', NULL, 'DEXTEROUS_HAND', 'SARM'),
                ('MODULE', 'RP1.3-BAT-018', '动力电池包', 'MAINTENANCE', 'SC-M-018', NULL, 'BATTERY', 'BAT'),
                ('MODULE', 'RP1.3-CHEST-006', '胸腔承载框架', 'ACTIVE', 'SC-M-006', NULL, 'CHEST_FRAME', 'CHEST'),
                ('MODULE', 'RP1.3-UPPER-012', '上肢联动总成', 'ACTIVE', 'SC-M-012', NULL, 'UPPER_BODY', 'UPPER'),
                ('MODULE', 'RP1.3-LOWER-017', '下肢联动总成', 'ACTIVE', 'SC-M-017', NULL, 'LOWER_BODY', 'LOWER'),
                ('MODULE', 'RP1.3-HEAD-004', '头部感知总成', 'ACTIVE', 'SC-M-004', NULL, 'HEAD_ASSEMBLY', 'HEAD')
            ) AS assets(kind, code, name, lifecycle, serial, model, module_type, target_part_code)
            WHERE assets.kind = kind_row.kind
        LOOP
            v_asset_index := v_asset_index + 1;

            INSERT INTO test.asset(
                asset_code, asset_kind, product_family, batch_id, serial_number,
                lifecycle_status, display_name, notes
            ) VALUES (
                asset_row.code, kind_row.kind, 'RP1-SHOWCASE', v_batch,
                asset_row.serial, asset_row.lifecycle, asset_row.name,
                'SHOWCASE development data'
            )
            ON CONFLICT (asset_code) DO UPDATE SET
                lifecycle_status = EXCLUDED.lifecycle_status,
                display_name = EXCLUDED.display_name,
                batch_id = EXCLUDED.batch_id,
                notes = EXCLUDED.notes,
                voided = false,
                updated_at = clock_timestamp()
            RETURNING id INTO v_asset;

            IF kind_row.kind = 'WHOLE_MACHINE' THEN
                INSERT INTO test.whole_machine_profile(
                    asset_id, model, platform_generation, nominal_payload_kg
                ) VALUES (v_asset, asset_row.model, 'RP1-G2', 18.5)
                ON CONFLICT (asset_id) DO UPDATE SET
                    model = EXCLUDED.model,
                    platform_generation = EXCLUDED.platform_generation,
                    updated_at = clock_timestamp();
            ELSE
                INSERT INTO test.module_profile(
                    asset_id, module_type, part_number, rated_specification,
                    target_part_code
                ) VALUES (
                    v_asset, asset_row.module_type, asset_row.serial,
                    jsonb_build_object('seed', 'SHOWCASE', 'rated_cycles', 100000),
                    asset_row.target_part_code
                )
                ON CONFLICT (asset_id) DO UPDATE SET
                    module_type = EXCLUDED.module_type,
                    rated_specification = EXCLUDED.rated_specification,
                    target_part_code = EXCLUDED.target_part_code,
                    updated_at = clock_timestamp();
            END IF;

            INSERT INTO test.campaign_asset(campaign_id, asset_id, participation_role)
            VALUES (v_campaign, v_asset, 'PRIMARY')
            ON CONFLICT (campaign_id, asset_id) DO UPDATE SET
                participation_role = 'PRIMARY',
                left_at = NULL;

            INSERT INTO test.configuration_snapshot(
                asset_id, fingerprint, hardware_manifest, software_manifest,
                parameter_manifest, reliability_impact, captured_from,
                captured_by, effective_from
            ) VALUES (
                v_asset, 'SHOWCASE-CFG-' || asset_row.serial,
                jsonb_build_object('assembly', asset_row.name, 'revision', 'R' || v_asset_index),
                jsonb_build_object('release', 'rp1-showcase-2.6.' || v_asset_index),
                jsonb_build_object('payload_kg', 8.4 + v_asset_index, 'seed', 'SHOWCASE'),
                CASE WHEN v_asset_index = 4 THEN 'POTENTIALLY_SIGNIFICANT' ELSE 'EQUIVALENT' END,
                'SHOWCASE-SEED', v_actor, clock_timestamp() - interval '30 days'
            )
            ON CONFLICT (asset_id, fingerprint) DO UPDATE SET
                hardware_manifest = EXCLUDED.hardware_manifest,
                software_manifest = EXCLUDED.software_manifest,
                parameter_manifest = EXCLUDED.parameter_manifest,
                reliability_impact = EXCLUDED.reliability_impact,
                updated_at = clock_timestamp()
            RETURNING id INTO v_config;

            INSERT INTO test.test_cycle(
                cycle_code, campaign_id, asset_id, status,
                baseline_confirmed_at, started_at
            ) VALUES (
                'SHOWCASE-CYCLE-' || asset_row.serial,
                v_campaign, v_asset,
                CASE WHEN asset_row.lifecycle = 'PAUSED' THEN 'PAUSED' ELSE 'ACTIVE' END,
                clock_timestamp() - interval '45 days',
                clock_timestamp() - interval '45 days'
            )
            ON CONFLICT (cycle_code) DO UPDATE SET
                status = EXCLUDED.status,
                ended_at = NULL,
                updated_at = clock_timestamp()
            RETURNING id INTO v_cycle;

            INSERT INTO test.analysis_segment(
                campaign_id, cycle_id, configuration_snapshot_id, segment_no,
                reason, started_at
            ) VALUES (
                v_campaign, v_cycle, v_config, 1, 'ORIGINAL',
                clock_timestamp() - interval '45 days'
            )
            ON CONFLICT (cycle_id, segment_no) DO UPDATE SET
                configuration_snapshot_id = EXCLUDED.configuration_snapshot_id,
                ended_at = NULL,
                updated_at = clock_timestamp()
            RETURNING id INTO v_segment;

            v_health_status := CASE v_asset_index
                WHEN 1 THEN 'NORMAL'
                WHEN 2 THEN 'ATTENTION'
                WHEN 3 THEN 'NORMAL'
                ELSE 'ABNORMAL'
            END;
            SELECT id INTO v_health
            FROM health.health_check_run
            WHERE campaign_id = v_campaign
              AND asset_id = v_asset
              AND evidence_summary ->> 'seed' = 'SHOWCASE'
            ORDER BY id
            LIMIT 1;
            IF v_health IS NULL THEN
                INSERT INTO health.health_check_run(
                    campaign_id, asset_id, cycle_id, segment_id, policy_id,
                    scheduled_at, started_at, completed_at, status,
                    quality_status, health_status, evidence_summary
                ) VALUES (
                    v_campaign, v_asset, v_cycle, v_segment, v_policy,
                    clock_timestamp() - interval '1 day',
                    clock_timestamp() - interval '1 day',
                    clock_timestamp() - interval '23 hours',
                    'COMPLETE', 'VALID', v_health_status,
                    jsonb_build_object('seed', 'SHOWCASE', 'checks', 12, 'available', 11)
                );
            ELSE
                UPDATE health.health_check_run
                SET health_status = v_health_status,
                    quality_status = 'VALID',
                    status = 'COMPLETE',
                    updated_at = clock_timestamp()
                WHERE id = v_health;
            END IF;
            v_health := NULL;

            FOR case_row IN
                SELECT tc.id, tc.case_code, tcv.id AS version_id,
                       row_number() OVER (ORDER BY tc.case_code) AS case_index
                FROM catalog.test_case tc
                JOIN catalog.test_case_version tcv
                  ON tcv.test_case_id = tc.id
                 AND (
                     tcv.version = 'CSV-20260918'
                     OR (
                         tc.case_code LIKE 'SHOWCASE-TELEMETRY-%'
                         AND tcv.version = 'SHOWCASE-1.0'
                     )
                 )
                 AND tcv.status = 'PUBLISHED'
                WHERE tc.asset_kind = kind_row.kind
                  AND tc.target_part_code = asset_row.target_part_code
                  AND tc.enabled
                ORDER BY tc.case_code
            LOOP
                v_status := CASE
                    WHEN asset_row.target_part_code = 'SARM'
                         AND case_row.case_index IN (1, 2) THEN 'RUNNING'
                    WHEN case_row.case_index = 1 AND asset_row.lifecycle = 'ACTIVE' THEN 'RUNNING'
                    WHEN case_row.case_index = 1 AND asset_row.lifecycle = 'PAUSED' THEN 'PAUSED'
                    WHEN case_row.case_index = 3 THEN 'BLOCKED'
                    WHEN case_row.case_index = 4 THEN 'FAILED'
                    ELSE 'COMPLETED'
                END;

                IF asset_row.target_part_code = 'SARM'
                   AND v_status = 'RUNNING' THEN
                    v_started := clock_timestamp() - interval '90 minutes';
                    v_ended := NULL;
                    v_received := clock_timestamp();
                ELSIF v_status IN ('RUNNING', 'PAUSED') THEN
                    v_started := clock_timestamp() - make_interval(hours => 2 + v_asset_index);
                    v_ended := NULL;
                    v_received := clock_timestamp() - interval '35 seconds';
                ELSE
                    v_started := clock_timestamp()
                        - make_interval(hours => case_row.case_index::integer * 150 + v_asset_index * 8);
                    v_seconds := (24 + v_asset_index * 4 + case_row.case_index * 7) * 3600;
                    v_ended := v_started + make_interval(secs => v_seconds::double precision);
                    v_received := v_ended + interval '3 minutes';
                END IF;

                INSERT INTO test.test_execution(
                    execution_code, campaign_id, cycle_id, asset_id,
                    configuration_snapshot_id, test_case_version_id, station_id,
                    operator_id, source_id, source_execution_key, status,
                    source_time, received_at, normalized_started_at, code_timestamp,
                    normalized_ended_at, clock_quality, data_quality
                ) VALUES (
                    test.compose_execution_code(
                        case_row.case_code, v_started, asset_row.code, kind_row.station_code
                    ),
                    v_campaign, v_cycle, v_asset, v_config, case_row.version_id,
                    v_station, v_actor, v_source,
                    'EXEC-' || asset_row.serial || '-' || case_row.case_index,
                    v_status, v_started, v_received, v_started, v_started, v_ended,
                    'VALID',
                    CASE WHEN v_status = 'BLOCKED' THEN 'PARTIAL' ELSE 'VALID' END
                )
                ON CONFLICT (source_id, source_execution_key)
                    WHERE source_id IS NOT NULL AND source_execution_key IS NOT NULL
                DO UPDATE SET
                    test_case_version_id = EXCLUDED.test_case_version_id,
                    status = EXCLUDED.status,
                    received_at = EXCLUDED.received_at,
                    normalized_started_at = EXCLUDED.normalized_started_at,
                    normalized_ended_at = EXCLUDED.normalized_ended_at,
                    data_quality = EXCLUDED.data_quality,
                    updated_at = clock_timestamp()
                RETURNING id INTO v_execution;

                FOR stage_row IN
                    SELECT * FROM (VALUES
                        ('PRECHECK', '预检', 1),
                        ('RAMP_LOAD', '负载爬升', 2),
                        ('STEADY_RUN', '稳态运行', 3),
                        ('RECOVERY', '恢复复测', 4)
                    ) AS stages(code, name, sequence_no)
                LOOP
                    INSERT INTO test.execution_stage(
                        campaign_id, execution_id, stage_code, stage_name, sequence_no,
                        status, source_time, received_at, normalized_started_at,
                        normalized_ended_at
                    ) VALUES (
                        v_campaign, v_execution, stage_row.code, stage_row.name,
                        stage_row.sequence_no,
                        CASE
                            WHEN stage_row.sequence_no < 4 THEN 'COMPLETED'
                            WHEN v_status = 'RUNNING' THEN 'RUNNING'
                            WHEN v_status = 'PAUSED' THEN 'PAUSED'
                            WHEN v_status = 'BLOCKED' THEN 'BLOCKED'
                            WHEN v_status = 'FAILED' THEN 'FAILED'
                            ELSE 'COMPLETED'
                        END,
                        v_started + make_interval(mins => stage_row.sequence_no * 20),
                        v_received,
                        v_started + make_interval(mins => (stage_row.sequence_no - 1) * 20),
                        CASE
                            WHEN stage_row.sequence_no = 4
                                 AND v_status IN ('RUNNING', 'PAUSED') THEN NULL
                            ELSE v_started + make_interval(mins => stage_row.sequence_no * 20)
                        END
                    )
                    ON CONFLICT (execution_id, sequence_no) DO UPDATE SET
                        stage_code = EXCLUDED.stage_code,
                        stage_name = EXCLUDED.stage_name,
                        status = EXCLUDED.status,
                        received_at = EXCLUDED.received_at,
                        normalized_started_at = EXCLUDED.normalized_started_at,
                        normalized_ended_at = EXCLUDED.normalized_ended_at,
                        updated_at = clock_timestamp()
                    RETURNING id INTO v_stage;
                END LOOP;

                SELECT id INTO v_stage
                FROM test.execution_stage
                WHERE execution_id = v_execution AND stage_code = 'STEADY_RUN';

                IF v_status IN ('RUNNING', 'PAUSED') THEN
                    v_seconds := 7200;
                    v_started := clock_timestamp() - interval '10 hours';
                    v_ended := v_started + interval '2 hours';
                END IF;

                INSERT INTO test.runtime_interval(
                    campaign_id, asset_id, cycle_id, execution_id, stage_id,
                    source_id, source_kind, source_record_key, source_time,
                    received_at, started_at, ended_at, active_seconds,
                    clock_quality, data_quality, source_method
                ) VALUES (
                    v_campaign, v_asset, v_cycle, v_execution, v_stage,
                    v_source, 'NATIVE',
                    'RUNTIME-' || asset_row.serial || '-' || case_row.case_index,
                    v_ended, v_received, v_started, v_ended, v_seconds,
                    'VALID',
                    CASE WHEN case_row.case_index = 3 THEN 'PARTIAL' ELSE 'VALID' END,
                    'SHOWCASE-SEED'
                )
                ON CONFLICT (source_id, source_record_key)
                    WHERE source_id IS NOT NULL AND source_record_key IS NOT NULL
                DO UPDATE SET
                    received_at = EXCLUDED.received_at,
                    started_at = EXCLUDED.started_at,
                    ended_at = EXCLUDED.ended_at,
                    active_seconds = EXCLUDED.active_seconds,
                    data_quality = EXCLUDED.data_quality,
                    updated_at = clock_timestamp()
                RETURNING id INTO v_interval;

                INSERT INTO reliability.exposure_assessment(
                    campaign_id, asset_id, runtime_interval_id,
                    configuration_snapshot_id, eligible, assignment_status,
                    quality_status, assessment_method, assessment_rule_version,
                    exclusion_reason, assessed_by, assessed_at
                ) VALUES (
                    v_campaign, v_asset, v_interval, v_config,
                    case_row.case_index NOT IN (3, 4),
                    CASE WHEN case_row.case_index = 4
                        THEN 'PENDING_CONFIG_REVIEW' ELSE 'CONFIRMED' END,
                    'VALID', 'SHOWCASE-SEED', 'SHOWCASE-1.0',
                    CASE WHEN case_row.case_index = 3
                        THEN '环境准备时间不计入有效暴露' ELSE '' END,
                    v_actor, clock_timestamp()
                )
                ON CONFLICT (runtime_interval_id) DO UPDATE SET
                    eligible = EXCLUDED.eligible,
                    assignment_status = EXCLUDED.assignment_status,
                    exclusion_reason = EXCLUDED.exclusion_reason,
                    assessed_at = EXCLUDED.assessed_at,
                    updated_at = clock_timestamp()
                RETURNING id INTO v_assessment;

                INSERT INTO reliability.exposure_scope_assignment(
                    campaign_id, assessment_id, scope_id, inclusion_status,
                    rationale
                ) VALUES (
                    v_campaign, v_assessment, v_scope,
                    CASE case_row.case_index
                        WHEN 3 THEN 'EXCLUDED'
                        WHEN 4 THEN 'PENDING'
                        ELSE 'INCLUDED'
                    END,
                    CASE case_row.case_index
                        WHEN 3 THEN 'Showcase 排除口径'
                        WHEN 4 THEN 'Showcase 待确认口径'
                        ELSE 'Showcase 有效暴露'
                    END
                )
                ON CONFLICT (assessment_id, scope_id) DO UPDATE SET
                    inclusion_status = EXCLUDED.inclusion_status,
                    rationale = EXCLUDED.rationale,
                    updated_at = clock_timestamp();

                INSERT INTO test.test_event(
                    campaign_id, asset_id, cycle_id, execution_id, stage_id,
                    event_type, source_time, received_at, normalized_time,
                    clock_quality, data_quality, source_id, source_event_key, payload
                ) VALUES (
                    v_campaign, v_asset, v_cycle, v_execution, v_stage,
                    CASE
                        WHEN v_status = 'FAILED' THEN 'ERROR_RAISED'
                        WHEN v_status = 'BLOCKED' THEN 'EXECUTION_BLOCKED'
                        WHEN v_status = 'RUNNING' THEN 'HEARTBEAT'
                        ELSE 'EXECUTION_CHECKPOINT'
                    END,
                    coalesce(v_ended, clock_timestamp() - interval '2 minutes'),
                    v_received,
                    coalesce(v_ended, clock_timestamp() - interval '2 minutes'),
                    'VALID',
                    CASE WHEN v_status = 'BLOCKED' THEN 'PARTIAL' ELSE 'VALID' END,
                    v_source,
                    'EVENT-' || asset_row.serial || '-' || case_row.case_index,
                    jsonb_build_object(
                        'seed', 'SHOWCASE',
                        'message', CASE
                            WHEN v_status = 'FAILED' THEN '驱动温升超过测试阈值'
                            WHEN v_status = 'BLOCKED' THEN '等待配置归属复核'
                            WHEN v_status = 'RUNNING' THEN '采集链路正常'
                            ELSE '阶段证据已归档'
                        END
                    )
                )
                ON CONFLICT (source_id, source_event_key)
                    WHERE source_id IS NOT NULL AND source_event_key IS NOT NULL
                DO UPDATE SET
                    event_type = EXCLUDED.event_type,
                    received_at = EXCLUDED.received_at,
                    normalized_time = EXCLUDED.normalized_time,
                    data_quality = EXCLUDED.data_quality,
                    payload = EXCLUDED.payload,
                    updated_at = clock_timestamp()
                RETURNING id INTO v_event;

                IF v_status IN ('FAILED', 'BLOCKED') THEN
                    INSERT INTO reliability.interruption(
                        campaign_id, asset_id, event_id, classification,
                        review_status, primary_error_code_id, source_raw_error,
                        started_at, recovered_at, relevance_reason,
                        confirmed_by, confirmed_at, failure_group_key
                    ) VALUES (
                        v_campaign, v_asset, v_event,
                        CASE
                            WHEN v_status = 'FAILED' AND kind_row.kind = 'MODULE'
                                THEN 'NON_RELEVANT'
                            WHEN v_status = 'FAILED' THEN 'FAILED'
                            ELSE 'BLOCKED'
                        END,
                        CASE WHEN v_status = 'FAILED' THEN 'CONFIRMED' ELSE 'PENDING' END,
                        CASE WHEN v_status = 'FAILED' AND kind_row.kind = 'WHOLE_MACHINE'
                            THEN v_error_code ELSE NULL END,
                        CASE WHEN v_status = 'BLOCKED' THEN 'SHOWCASE-CONFIG-REVIEW' ELSE '' END,
                        coalesce(v_ended, clock_timestamp() - interval '30 minutes'),
                        CASE WHEN v_status = 'FAILED'
                            THEN coalesce(v_ended, clock_timestamp()) + interval '40 minutes'
                            ELSE NULL END,
                        CASE WHEN v_status = 'FAILED' AND kind_row.kind = 'MODULE'
                            THEN '模块保护停机，不计入 OVERALL MTBF'
                            ELSE 'SHOWCASE reliability review' END,
                        CASE WHEN v_status = 'FAILED' THEN v_actor ELSE NULL END,
                        CASE WHEN v_status = 'FAILED' THEN clock_timestamp() ELSE NULL END,
                        'SHOWCASE-GROUP-' || asset_row.serial || '-' || case_row.case_index
                    )
                    ON CONFLICT (event_id) DO UPDATE SET
                        classification = EXCLUDED.classification,
                        review_status = EXCLUDED.review_status,
                        primary_error_code_id = EXCLUDED.primary_error_code_id,
                        source_raw_error = EXCLUDED.source_raw_error,
                        confirmed_by = EXCLUDED.confirmed_by,
                        confirmed_at = EXCLUDED.confirmed_at,
                        updated_at = clock_timestamp()
                    RETURNING id INTO v_interruption;

                    INSERT INTO reliability.interruption_scope_assignment(
                        interruption_id, scope_id, status, rationale, assigned_by
                    ) VALUES (
                        v_interruption, v_scope,
                        CASE
                            WHEN v_status = 'FAILED' AND kind_row.kind = 'MODULE' THEN 'EXCLUDED'
                            WHEN v_status = 'FAILED' THEN 'INCLUDED'
                            ELSE 'PENDING'
                        END,
                        CASE
                            WHEN v_status = 'FAILED' AND kind_row.kind = 'MODULE'
                                THEN 'Showcase 非相关保护停机'
                            WHEN v_status = 'FAILED' THEN 'Showcase 相关故障'
                            ELSE 'Showcase Blocked 待复核'
                        END,
                        v_actor
                    )
                    ON CONFLICT (interruption_id, scope_id) DO UPDATE SET
                        status = EXCLUDED.status,
                        rationale = EXCLUDED.rationale;
                END IF;

                IF case_row.case_index = 1 THEN
                    INSERT INTO integration.artifact(
                        campaign_id, asset_id, cycle_id, execution_id, kind,
                        availability_status, object_key, source_location,
                        file_name, mime_type, size_bytes, sha256
                    ) VALUES (
                        v_campaign, v_asset, v_cycle, v_execution, 'RAW_LOG_BUNDLE',
                        'AVAILABLE',
                        'showcase/' || lower(asset_row.serial) || '/execution-evidence.zip',
                        'SHOWCASE-SEED',
                        asset_row.serial || '-evidence.zip',
                        'application/zip', 18432000 + v_asset_index * 102400,
                        repeat(v_asset_index::text, 64)
                    )
                    ON CONFLICT (object_key) DO UPDATE SET
                        availability_status = 'AVAILABLE',
                        size_bytes = EXCLUDED.size_bytes,
                        updated_at = clock_timestamp();

                    WITH generated AS (
                        SELECT
                            mv.id AS metric_version_id,
                            metric.code AS metric_code,
                            metric.unit,
                            stage.id AS stage_id,
                            stage.stage_code,
                            stage.sequence_no,
                            point_no,
                            stage.normalized_started_at
                                + make_interval(secs => point_no * 60) AS observed_at,
                            CASE metric.code
                                WHEN 'SHOWCASE-JOINT-TEMP' THEN
                                    CASE stage.sequence_no
                                        WHEN 1 THEN 33.5 + v_asset_index * 0.35 + point_no * 0.08
                                        WHEN 2 THEN 35.2 + v_asset_index * 0.35 + point_no * 0.42
                                        WHEN 3 THEN 40.0 + v_asset_index * 0.35 + point_no * 0.18
                                            + sin(point_no * 0.75) * 0.55
                                        ELSE 42.4 + v_asset_index * 0.35 - point_no * 0.48
                                            + sin(point_no * 0.55) * 0.25
                                    END
                                WHEN 'SHOWCASE-JOINT-CURRENT' THEN
                                    CASE stage.sequence_no
                                        WHEN 1 THEN 2.1 + v_asset_index * 0.08
                                            + sin(point_no * 0.9) * 0.16
                                        WHEN 2 THEN 3.0 + v_asset_index * 0.08 + point_no * 0.31
                                            + sin(point_no * 0.7) * 0.24
                                        WHEN 3 THEN 6.4 + v_asset_index * 0.08
                                            + sin(point_no * 0.95) * 0.55
                                        ELSE 5.0 + v_asset_index * 0.08 - point_no * 0.19
                                            + sin(point_no * 0.6) * 0.18
                                    END
                                ELSE
                                    CASE stage.sequence_no
                                        WHEN 1 THEN 0.24 + v_asset_index * 0.01
                                            + abs(sin(point_no * 0.8)) * 0.04
                                        WHEN 2 THEN 0.31 + v_asset_index * 0.01 + point_no * 0.018
                                            + abs(sin(point_no * 0.7)) * 0.05
                                        WHEN 3 THEN 0.54 + v_asset_index * 0.01
                                            + abs(sin(point_no * 1.1)) * 0.11
                                        ELSE 0.48 + v_asset_index * 0.01 - point_no * 0.017
                                            + abs(sin(point_no * 0.65)) * 0.04
                                    END
                            END::numeric AS metric_value
                        FROM test.execution_stage stage
                        CROSS JOIN (
                            VALUES
                                ('SHOWCASE-JOINT-TEMP', '°C'),
                                ('SHOWCASE-JOINT-CURRENT', 'A'),
                                ('SHOWCASE-VIBRATION-RMS', 'mm/s')
                        ) AS metric(code, unit)
                        JOIN catalog.metric_definition md
                          ON md.metric_code = metric.code
                        JOIN catalog.metric_version mv
                          ON mv.metric_definition_id = md.id
                         AND mv.version = 'SHOWCASE-1.0'
                        CROSS JOIN generate_series(0, 11) AS point_no
                        WHERE stage.execution_id = v_execution
                    )
                    INSERT INTO health.metric_observation(
                        campaign_id, metric_version_id, asset_id, cycle_id,
                        segment_id, execution_id, stage_id,
                        configuration_snapshot_id, observed_at,
                        exposure_coordinates, canonical_numeric_value,
                        original_numeric_value, original_unit,
                        conversion_method, quality_status, source_kind,
                        analysis_eligible, raw_data_reference, calculation_run_key
                    )
                    SELECT
                        v_campaign, generated.metric_version_id, v_asset, v_cycle,
                        v_segment, v_execution, generated.stage_id, v_config,
                        generated.observed_at,
                        jsonb_build_object(
                            'stage_elapsed_seconds', generated.point_no * 60,
                            'stage_progress_percent', generated.point_no * 100.0 / 11,
                            'channel', 'primary'
                        ),
                        generated.metric_value, generated.metric_value, generated.unit,
                        'identity',
                        CASE WHEN generated.sequence_no = 3 AND generated.point_no = 10
                            THEN 'PARTIAL' ELSE 'VALID' END,
                        'NATIVE', true,
                        jsonb_build_object(
                            'seed', 'SHOWCASE',
                            'signal', 'deterministic-continuous-sample'
                        ),
                        'SHOWCASE-TREND-' || asset_row.serial || '-'
                            || generated.metric_code || '-' || generated.stage_code || '-'
                            || lpad(generated.point_no::text, 3, '0')
                    FROM generated
                    WHERE NOT EXISTS (
                        SELECT 1
                        FROM health.metric_observation existing
                        WHERE existing.calculation_run_key =
                            'SHOWCASE-TREND-' || asset_row.serial || '-'
                            || generated.metric_code || '-' || generated.stage_code || '-'
                            || lpad(generated.point_no::text, 3, '0')
                    );
                END IF;
            END LOOP;
        END LOOP;

        SELECT id INTO v_method
        FROM reliability.statistics_method
        WHERE implementation_key = 'mtbf.poisson.exposure_estimate.v1'
          AND status = 'PUBLISHED'
        ORDER BY id DESC
        LIMIT 1;

        INSERT INTO reliability.campaign_mtbf_config(
            campaign_id, scope_id, method_id, target_seconds,
            confidence_levels, enabled, settings, created_by, updated_by
        ) VALUES (
            v_campaign, v_scope, v_method,
            CASE WHEN kind_row.kind = 'WHOLE_MACHINE'
                THEN 7200000 ELSE 5400000 END,
            ARRAY[0.70, 0.90]::numeric(6,5)[], true,
            jsonb_build_object('seed', 'SHOWCASE', 'display', 'visual-review'),
            v_actor, v_actor
        )
        ON CONFLICT (campaign_id, scope_id) DO UPDATE SET
            target_seconds = EXCLUDED.target_seconds,
            confidence_levels = EXCLUDED.confidence_levels,
            enabled = true,
            settings = EXCLUDED.settings,
            updated_by = v_actor,
            updated_at = clock_timestamp();

        SELECT id INTO v_run
        FROM reliability.calculation_run
        WHERE input_hash = 'SHOWCASE-MTBF-' || kind_row.kind || '-V1'
        ORDER BY id
        LIMIT 1;
        IF v_run IS NULL THEN
            INSERT INTO reliability.calculation_run(
                population_id, campaign_id, scope_id, method_id, status,
                data_cutoff_at, knowledge_as_of_at, input_hash, input_summary,
                method_parameters, output_summary, evidence_summary,
                implementation_version, initiated_by, started_at, completed_at
            ) VALUES (
                v_population, v_campaign, v_scope, v_method, 'SUCCEEDED',
                clock_timestamp() - interval '2 minutes', clock_timestamp(),
                'SHOWCASE-MTBF-' || kind_row.kind || '-V1',
                jsonb_build_object('seed', 'SHOWCASE', 'asset_count', 4),
                jsonb_build_object('confidence_levels', jsonb_build_array(0.70, 0.90)),
                jsonb_build_object('status', 'SHOWCASE_RESULT'),
                jsonb_build_object('complete', true, 'seed', 'SHOWCASE'),
                'mtbf.poisson.exposure_estimate.v1', v_actor,
                clock_timestamp() - interval '3 minutes',
                clock_timestamp() - interval '2 minutes'
            )
            RETURNING id INTO v_run;
        ELSE
            UPDATE reliability.calculation_run
            SET status = 'SUCCEEDED',
                data_cutoff_at = clock_timestamp() - interval '2 minutes',
                knowledge_as_of_at = clock_timestamp(),
                completed_at = clock_timestamp() - interval '2 minutes'
            WHERE id = v_run;
        END IF;

        INSERT INTO reliability.current_mtbf_result(
            population_id, campaign_id, scope_id, calculation_run_id,
            exposure_seconds, relevant_failure_count, pending_block_count,
            pending_exposure_seconds, excluded_exposure_seconds,
            point_estimate_hours, no_failure_exposure_hours, result_payload,
            lower_70_hours, lower_90_hours, data_cutoff_at, calculated_at
        ) VALUES (
            v_population, v_campaign, v_scope, v_run,
            CASE WHEN kind_row.kind = 'WHOLE_MACHINE'
                THEN 5574960 ELSE 4409280 END,
            CASE WHEN kind_row.kind = 'WHOLE_MACHINE' THEN 2 ELSE 0 END,
            1,
            CASE WHEN kind_row.kind = 'WHOLE_MACHINE' THEN 172800 ELSE 129600 END,
            CASE WHEN kind_row.kind = 'WHOLE_MACHINE' THEN 284400 ELSE 230400 END,
            CASE WHEN kind_row.kind = 'WHOLE_MACHINE' THEN 774.3 ELSE NULL END,
            CASE WHEN kind_row.kind = 'MODULE' THEN 1224.8 ELSE NULL END,
            jsonb_build_object(
                'seed', 'SHOWCASE',
                'point_estimate_status',
                CASE WHEN kind_row.kind = 'WHOLE_MACHINE'
                    THEN 'FINITE_ESTIMATE' ELSE 'NO_FINITE_ESTIMATE' END,
                'confidence_bounds',
                CASE WHEN kind_row.kind = 'WHOLE_MACHINE'
                    THEN jsonb_build_object('0.7', 466.1, '0.9', 267.2)
                    ELSE jsonb_build_object('0.7', 1017.2, '0.9', 531.9)
                END
            ),
            CASE WHEN kind_row.kind = 'WHOLE_MACHINE' THEN 466.1 ELSE 1017.2 END,
            CASE WHEN kind_row.kind = 'WHOLE_MACHINE' THEN 267.2 ELSE 531.9 END,
            clock_timestamp() - interval '2 minutes',
            clock_timestamp() - interval '2 minutes'
        )
        ON CONFLICT (campaign_id, scope_id) WHERE campaign_id IS NOT NULL
        DO UPDATE SET
            population_id = EXCLUDED.population_id,
            calculation_run_id = EXCLUDED.calculation_run_id,
            exposure_seconds = EXCLUDED.exposure_seconds,
            relevant_failure_count = EXCLUDED.relevant_failure_count,
            pending_block_count = EXCLUDED.pending_block_count,
            pending_exposure_seconds = EXCLUDED.pending_exposure_seconds,
            excluded_exposure_seconds = EXCLUDED.excluded_exposure_seconds,
            point_estimate_hours = EXCLUDED.point_estimate_hours,
            no_failure_exposure_hours = EXCLUDED.no_failure_exposure_hours,
            result_payload = EXCLUDED.result_payload,
            lower_70_hours = EXCLUDED.lower_70_hours,
            lower_90_hours = EXCLUDED.lower_90_hours,
            data_cutoff_at = EXCLUDED.data_cutoff_at,
            calculated_at = EXCLUDED.calculated_at,
            updated_at = clock_timestamp();

        INSERT INTO reliability.current_conclusion(
            population_id, scope_id, calculation_run_id, status,
            conclusion_payload, evidence_completeness, reviewed_by,
            reviewed_at, published_by, published_at
        ) VALUES (
            v_population, v_scope, v_run,
            CASE WHEN kind_row.kind = 'WHOLE_MACHINE' THEN 'REVIEWED' ELSE 'DRAFT' END,
            jsonb_build_object(
                'seed', 'SHOWCASE',
                'statement', CASE WHEN kind_row.kind = 'WHOLE_MACHINE'
                    THEN '观测结果已完成工程复核，等待正式发布'
                    ELSE '零故障观测仅形成单侧置信下界，不形成有限点估计' END
            ),
            jsonb_build_object('available', 18, 'required', 20, 'seed', 'SHOWCASE'),
            CASE WHEN kind_row.kind = 'WHOLE_MACHINE' THEN v_actor ELSE NULL END,
            CASE WHEN kind_row.kind = 'WHOLE_MACHINE' THEN clock_timestamp() ELSE NULL END,
            NULL, NULL
        )
        ON CONFLICT (population_id, scope_id) DO UPDATE SET
            calculation_run_id = EXCLUDED.calculation_run_id,
            status = EXCLUDED.status,
            conclusion_payload = EXCLUDED.conclusion_payload,
            evidence_completeness = EXCLUDED.evidence_completeness,
            reviewed_by = EXCLUDED.reviewed_by,
            reviewed_at = EXCLUDED.reviewed_at,
            updated_at = clock_timestamp();
    END LOOP;
END
$showcase$;

-- Keep the representative steady-run stages fixed so repeated seeding cannot
-- move a series header away from its already persisted sample timestamps.
WITH representative_execution AS (
    SELECT
        execution.id AS execution_id,
        row_number() OVER (ORDER BY asset.asset_code) AS asset_order
    FROM test.test_execution execution
    JOIN test.asset asset ON asset.id = execution.asset_id
    JOIN integration.ingestion_source source ON source.id = execution.source_id
    WHERE source.source_code = 'SHOWCASE-COLLECTOR'
      AND asset.product_family = 'RP1-SHOWCASE'
      AND execution.source_execution_key =
          'EXEC-' || asset.serial_number || '-1'
),
fixed_stage AS (
    SELECT
        execution_id,
        timestamptz '2026-09-01 08:00:00+08'
            + make_interval(days => asset_order::integer) AS started_at
    FROM representative_execution
)
UPDATE test.execution_stage stage
SET source_time = fixed_stage.started_at + interval '20 minutes',
    received_at = fixed_stage.started_at + interval '20 minutes 5 seconds',
    normalized_started_at = fixed_stage.started_at,
    normalized_ended_at = fixed_stage.started_at + interval '20 minutes',
    clock_quality = 'VALID',
    updated_at = clock_timestamp()
FROM fixed_stage
WHERE stage.execution_id = fixed_stage.execution_id
  AND stage.stage_code = 'STEADY_RUN';

-- One relational series header is created for each applicable
-- asset/subject/metric/motion-cycle combination. Battery channels and frame
-- points intentionally expose only physically meaningful metric subsets;
-- every asset also has a JOINT subject carrying all eight registered metrics.
WITH source_row AS (
    SELECT id
    FROM integration.ingestion_source
    WHERE source_code = 'SHOWCASE-COLLECTOR'
),
asset_context AS (
    SELECT
        asset.id AS asset_id,
        asset.asset_code,
        asset.serial_number,
        execution.campaign_id,
        execution.cycle_id,
        segment.id AS segment_id,
        execution.id AS execution_id,
        stage.id AS stage_id,
        execution.configuration_snapshot_id,
        stage.normalized_started_at AS stage_started_at,
        CASE
            WHEN asset.asset_kind = 'WHOLE_MACHINE' THEN 'SYS'
            ELSE profile.target_part_code
        END AS target_part_code,
        CASE asset.asset_code
            WHEN 'RP1.3-SYS-023' THEN 'NORMAL'
            WHEN 'RP1.3-SYS-031' THEN 'TRACKING_DRIFT'
            WHEN 'RP1.3-SYS-038' THEN 'QUALITY_GAP'
            WHEN 'RP1.3-SYS-044' THEN 'TEMPERATURE_RISE'
            WHEN 'RP1.3-SLEG-014' THEN 'NORMAL'
            WHEN 'RP1.3-SLEG-009' THEN 'IMPACT_TORQUE'
            WHEN 'RP1.3-SARM-022' THEN 'TRACKING_DRIFT'
            WHEN 'RP1.3-BAT-018' THEN 'TEMPERATURE_RISE'
            WHEN 'RP1.3-CHEST-006' THEN 'QUALITY_GAP'
            WHEN 'RP1.3-UPPER-012' THEN 'VIBRATION'
            WHEN 'RP1.3-LOWER-017' THEN 'IMPACT_TORQUE'
            WHEN 'RP1.3-HEAD-004' THEN 'VIBRATION'
        END AS scenario
    FROM test.asset asset
    LEFT JOIN test.module_profile profile ON profile.asset_id = asset.id
    JOIN test.test_execution execution
      ON execution.asset_id = asset.id
     AND execution.source_execution_key =
         'EXEC-' || asset.serial_number || '-1'
    JOIN integration.ingestion_source source
      ON source.id = execution.source_id
     AND source.source_code = 'SHOWCASE-COLLECTOR'
    JOIN test.execution_stage stage
      ON stage.execution_id = execution.id
     AND stage.stage_code = 'STEADY_RUN'
    JOIN test.analysis_segment segment
      ON segment.campaign_id = execution.campaign_id
     AND segment.cycle_id = execution.cycle_id
     AND segment.configuration_snapshot_id =
         execution.configuration_snapshot_id
    WHERE asset.product_family = 'RP1-SHOWCASE'
),
metric_catalog AS (
    SELECT
        version.id AS metric_version_id,
        definition.metric_code
    FROM catalog.metric_definition definition
    JOIN catalog.metric_version version
      ON version.metric_definition_id = definition.id
     AND version.version = '1.0.0'
     AND version.status = 'PUBLISHED'
    WHERE definition.metric_code IN (
        'JOINT-TARGET-POSITION',
        'JOINT-ACTUAL-POSITION',
        'JOINT-TRACKING-ERROR',
        'JOINT-VELOCITY',
        'JOINT-TORQUE',
        'JOINT-CURRENT',
        'JOINT-TEMPERATURE',
        'JOINT-VIBRATION-RMS'
    )
),
series_rows AS (
    SELECT
        context.*,
        subject.subject_code,
        subject.subject_kind,
        metric.metric_version_id,
        metric.metric_code,
        motion_cycle.cycle_index,
        context.stage_started_at
            + make_interval(mins => motion_cycle.cycle_index * 3)
            AS series_started_at
    FROM asset_context context
    JOIN catalog.telemetry_subject subject
      ON subject.target_part_code = context.target_part_code
     AND subject.enabled
    CROSS JOIN metric_catalog metric
    CROSS JOIN generate_series(1, 3) AS motion_cycle(cycle_index)
    WHERE subject.subject_kind = 'JOINT'
       OR (
           subject.subject_kind = 'BATTERY_CHANNEL'
           AND metric.metric_code IN (
               'JOINT-CURRENT',
               'JOINT-TEMPERATURE',
               'JOINT-VIBRATION-RMS'
           )
       )
       OR (
           subject.subject_kind = 'FRAME_POINT'
           AND metric.metric_code IN (
               'JOINT-TORQUE',
               'JOINT-TEMPERATURE',
               'JOINT-VIBRATION-RMS'
           )
       )
)
INSERT INTO health.metric_series(
    campaign_id,
    asset_id,
    cycle_id,
    segment_id,
    execution_id,
    stage_id,
    configuration_snapshot_id,
    metric_version_id,
    subject_code,
    series_kind,
    cycle_index,
    started_at,
    ended_at,
    point_count,
    sampling_interval_ms,
    downsample_method,
    quality_status,
    raw_data_reference,
    source_id,
    source_series_key
)
SELECT
    series_rows.campaign_id,
    series_rows.asset_id,
    series_rows.cycle_id,
    series_rows.segment_id,
    series_rows.execution_id,
    series_rows.stage_id,
    series_rows.configuration_snapshot_id,
    series_rows.metric_version_id,
    series_rows.subject_code,
    'MOTION_CYCLE',
    series_rows.cycle_index,
    series_rows.series_started_at,
    series_rows.series_started_at + interval '1.98 seconds',
    100,
    20.000,
    'DETERMINISTIC_UNIFORM_100',
    CASE
        WHEN series_rows.scenario = 'QUALITY_GAP' THEN 'PARTIAL'
        ELSE 'VALID'
    END,
    jsonb_build_object(
        'seed', 'SHOWCASE',
        'scenario', series_rows.scenario,
        'sourceSignal', 'deterministic-correlated-waveform-v1'
    ),
    source_row.id,
    'SHOWCASE-TELEMETRY-' || series_rows.asset_code
        || '-' || series_rows.subject_code
        || '-' || series_rows.metric_code
        || '-C' || series_rows.cycle_index
FROM series_rows
CROSS JOIN source_row
ON CONFLICT (source_id, source_series_key)
    WHERE source_id IS NOT NULL AND source_series_key IS NOT NULL
DO UPDATE SET
    point_count = EXCLUDED.point_count,
    sampling_interval_ms = EXCLUDED.sampling_interval_ms,
    downsample_method = EXCLUDED.downsample_method,
    quality_status = EXCLUDED.quality_status,
    raw_data_reference = EXCLUDED.raw_data_reference,
    started_at = EXCLUDED.started_at,
    ended_at = EXCLUDED.ended_at,
    updated_at = clock_timestamp();

-- Deterministic correlated signals. Position, tracking error and actual
-- position share a strict algebraic relationship; velocity, torque, current,
-- temperature and vibration derive from the same phase and load components,
-- while scenario terms introduce controlled faults without random drift.
-- A single coalesced recomputation is already queued by each series header;
-- suppress the per-point recompute trigger during this administrative bulk
-- load while retaining the normal row audit trigger.
ALTER TABLE health.metric_observation
    DISABLE TRIGGER trg_enqueue_recompute;

WITH source_row AS (
    SELECT id
    FROM integration.ingestion_source
    WHERE source_code = 'SHOWCASE-COLLECTOR'
),
series_scope AS (
    SELECT
        series.*,
        asset.asset_code,
        subject.display_order AS subject_order,
        definition.metric_code,
        version.canonical_unit,
        series.raw_data_reference ->> 'scenario' AS scenario,
        mod(right(asset.asset_code, 3)::integer, 11)::double precision
            AS asset_bias
    FROM health.metric_series series
    JOIN source_row ON source_row.id = series.source_id
    JOIN test.asset asset ON asset.id = series.asset_id
    JOIN catalog.telemetry_subject subject
      ON subject.subject_code = series.subject_code
    JOIN catalog.metric_version version
      ON version.id = series.metric_version_id
    JOIN catalog.metric_definition definition
      ON definition.id = version.metric_definition_id
    WHERE series.source_series_key LIKE 'SHOWCASE-TELEMETRY-%'
),
sample_grid AS (
    SELECT
        series_scope.*,
        sample.sample_index,
        2.0 * pi() * sample.sample_index / 100.0
            + (series_scope.subject_order - 1) * 0.21
            + (series_scope.cycle_index - 1) * 0.13 AS phase,
        18.0
            + series_scope.subject_order * 5.5
            + series_scope.asset_bias * 0.35 AS amplitude,
        exp(
            -power((sample.sample_index - 64.0) / 3.8, 2)
        ) AS impact_pulse,
        exp(
            -power((sample.sample_index - 48.0) / 5.0, 2)
        ) AS local_defect_pulse
    FROM series_scope
    CROSS JOIN generate_series(0, 99) AS sample(sample_index)
),
base_signals AS (
    SELECT
        sample_grid.*,
        amplitude * sin(phase)
            + amplitude * 0.08 * sin(2.0 * phase + 0.4)
            AS target_raw,
        0.16 * sin(3.0 * phase + subject_order * 0.3)
            + 0.05 * cos(5.0 * phase + asset_bias)
            + CASE
                WHEN scenario = 'TRACKING_DRIFT'
                    THEN 3.8 * sample_index / 99.0
                WHEN scenario = 'QUALITY_GAP'
                    THEN 1.9 * local_defect_pulse
                ELSE 0.0
              END
            + CASE
                WHEN scenario = 'IMPACT_TORQUE'
                    THEN 0.45 * impact_pulse
                ELSE 0.0
              END AS error_raw,
        amplitude * (
            4.6 * cos(phase)
            + 0.74 * cos(2.0 * phase + 0.4)
        ) + 1.4 * sin(4.0 * phase + asset_bias)
            AS velocity_raw
    FROM sample_grid
),
mechanical_signals AS (
    SELECT
        base_signals.*,
        round(target_raw::numeric, 6) AS target_position,
        round(error_raw::numeric, 6) AS tracking_error,
        round(velocity_raw::numeric, 6) AS velocity,
        round((
            4.4
            + abs(target_raw) * 0.032
            + abs(velocity_raw) * 0.019
            + 0.65 * sin(2.0 * phase + 0.8)
            + impact_pulse * CASE
                WHEN scenario = 'IMPACT_TORQUE' THEN 24.0
                ELSE 0.7
              END
        )::numeric, 6) AS torque
    FROM base_signals
),
electrical_signals AS (
    SELECT
        mechanical_signals.*,
        target_position + tracking_error AS actual_position,
        round((
            0.82
            + abs(torque::double precision) * 0.155
            + abs(velocity_raw) * 0.0028
            + 0.12 * sin(3.0 * phase + 0.2)
        )::numeric, 6) AS current_value
    FROM mechanical_signals
),
all_signals AS (
    SELECT
        electrical_signals.*,
        round((
            33.5
            + asset_bias * 0.18
            + cycle_index * 0.75
            + sample_index * 0.014
            + abs(current_value::double precision) * 0.31
            + 0.18 * sin(phase + 0.5)
            + CASE
                WHEN scenario = 'TEMPERATURE_RISE'
                    THEN sample_index * 0.105
                ELSE 0.0
              END
        )::numeric, 6) AS temperature_value,
        round((
            0.22
            + abs(velocity_raw) * 0.0016
            + abs(torque::double precision) * 0.011
            + 0.05 * abs(sin(6.0 * phase + subject_order))
            + CASE
                WHEN scenario = 'VIBRATION'
                    THEN 0.78 + 0.48 * abs(sin(11.0 * phase + 0.3))
                ELSE 0.0
              END
            + impact_pulse * CASE
                WHEN scenario = 'IMPACT_TORQUE' THEN 1.45
                ELSE 0.04
              END
        )::numeric, 6) AS vibration_value
    FROM electrical_signals
),
valued_points AS (
    SELECT
        all_signals.*,
        CASE metric_code
            WHEN 'JOINT-TARGET-POSITION' THEN target_position
            WHEN 'JOINT-ACTUAL-POSITION' THEN actual_position
            WHEN 'JOINT-TRACKING-ERROR' THEN tracking_error
            WHEN 'JOINT-VELOCITY' THEN velocity
            WHEN 'JOINT-TORQUE' THEN torque
            WHEN 'JOINT-CURRENT' THEN current_value
            WHEN 'JOINT-TEMPERATURE' THEN temperature_value
            WHEN 'JOINT-VIBRATION-RMS' THEN vibration_value
        END AS metric_value,
        CASE
            WHEN scenario = 'QUALITY_GAP'
             AND sample_index BETWEEN 42 AND 47
             AND metric_code IN (
                 'JOINT-ACTUAL-POSITION',
                 'JOINT-CURRENT',
                 'JOINT-VIBRATION-RMS'
             )
                THEN 'INVALID'
            WHEN scenario = 'QUALITY_GAP'
             AND sample_index BETWEEN 38 AND 52
                THEN 'PARTIAL'
            ELSE 'VALID'
        END AS point_quality
    FROM all_signals
)
INSERT INTO health.metric_observation(
    campaign_id,
    metric_version_id,
    asset_id,
    cycle_id,
    segment_id,
    execution_id,
    stage_id,
    configuration_snapshot_id,
    series_id,
    sample_index,
    observed_at,
    exposure_coordinates,
    canonical_numeric_value,
    original_numeric_value,
    original_unit,
    conversion_method,
    quality_status,
    source_kind,
    analysis_eligible,
    raw_data_reference,
    calculation_run_key
)
SELECT
    valued_points.campaign_id,
    valued_points.metric_version_id,
    valued_points.asset_id,
    valued_points.cycle_id,
    valued_points.segment_id,
    valued_points.execution_id,
    valued_points.stage_id,
    valued_points.configuration_snapshot_id,
    valued_points.id,
    valued_points.sample_index,
    valued_points.started_at
        + make_interval(
            secs => (
                valued_points.sample_index
                * valued_points.sampling_interval_ms
                / 1000.0
            )::double precision
        ),
    jsonb_build_object(
        'cycle_index', valued_points.cycle_index,
        'cycle_progress_percent', valued_points.sample_index * 100.0 / 99.0,
        'subject_code', valued_points.subject_code
    ),
    CASE
        WHEN valued_points.point_quality = 'INVALID' THEN NULL
        ELSE valued_points.metric_value
    END,
    CASE
        WHEN valued_points.point_quality = 'INVALID' THEN NULL
        ELSE valued_points.metric_value
    END,
    valued_points.canonical_unit,
    'identity',
    valued_points.point_quality,
    'NATIVE',
    valued_points.point_quality <> 'INVALID',
    jsonb_build_object(
        'seed', 'SHOWCASE',
        'scenario', valued_points.scenario,
        'downsampleMethod', valued_points.downsample_method
    ),
    valued_points.source_series_key
        || '-S' || lpad(valued_points.sample_index::text, 3, '0')
FROM valued_points
ON CONFLICT (series_id, sample_index)
    WHERE series_id IS NOT NULL
DO NOTHING;

ALTER TABLE health.metric_observation
    ENABLE TRIGGER trg_enqueue_recompute;

COMMIT;

\echo 'SHOWCASE seed complete: 12 assets, 804 motion-cycle series and 80,400 normalized telemetry points.'
