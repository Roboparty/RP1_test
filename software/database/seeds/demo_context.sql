-- Synthetic context only. Explicitly run after migrations; no executions inserted.
DO $demo$
DECLARE
    v_actor bigint;
    v_site bigint; v_lab bigint; v_station bigint;
    v_program bigint; v_campaign bigint; v_asset bigint;
    v_configuration bigint; v_cycle bigint; v_segment bigint;
BEGIN
    SELECT id INTO STRICT v_actor FROM iam.app_user WHERE username = 'local-admin';
    PERFORM iam.set_request_context(
        '00000000-0000-7000-8000-000000000002'::uuid,
        'open-source-demo-context', 'explicit synthetic demo initialization'
    );
    INSERT INTO test.site(site_code, name, location)
    VALUES ('DEMO-SYNTHETIC-SITE', '合成演示场地', 'simulation only')
    ON CONFLICT(site_code) DO UPDATE SET name = EXCLUDED.name RETURNING id INTO v_site;
    INSERT INTO test.lab(site_id, lab_code, name)
    VALUES (v_site, 'DEMO-SYNTHETIC-LAB', '合成演示实验室')
    ON CONFLICT(site_id, lab_code) DO UPDATE SET name = EXCLUDED.name RETURNING id INTO v_lab;
    INSERT INTO test.station(lab_id, station_code, name, station_type)
    VALUES (v_lab, 'DEMO-SYNTHETIC', '合成演示台架', 'WHOLE_MACHINE')
    ON CONFLICT(station_code) DO UPDATE SET name = EXCLUDED.name RETURNING id INTO v_station;
    INSERT INTO test.test_program(program_code, name, objective, status, owner_id)
    VALUES ('DEMO-SYNTHETIC', '合成测试演示', '软件链路演示；无实际机器人实验', 'ACTIVE', v_actor)
    ON CONFLICT(program_code) DO UPDATE SET name = EXCLUDED.name RETURNING id INTO v_program;
    INSERT INTO test.test_campaign(program_id, campaign_code, name, asset_kind, status)
    VALUES (v_program, 'DEMO-SYNTHETIC', '合成站立数据演示', 'WHOLE_MACHINE', 'ACTIVE')
    ON CONFLICT(program_id, campaign_code) DO UPDATE SET name = EXCLUDED.name RETURNING id INTO v_campaign;
    INSERT INTO test.asset(asset_code, asset_kind, product_family, serial_number, lifecycle_status, display_name, notes)
    VALUES ('RP0.0-SYS-001', 'WHOLE_MACHINE', 'SYNTHETIC', 'DEMO-SYNTHETIC-001', 'ACTIVE', '合成机器人', 'Generated example; no real sample')
    ON CONFLICT(asset_code) DO UPDATE SET notes = EXCLUDED.notes RETURNING id INTO v_asset;
    INSERT INTO test.whole_machine_profile(asset_id, model, platform_generation)
    VALUES (v_asset, 'SYNTHETIC', 'DEMO') ON CONFLICT(asset_id) DO NOTHING;
    INSERT INTO test.campaign_asset(campaign_id, asset_id)
    VALUES (v_campaign, v_asset) ON CONFLICT DO NOTHING;
    INSERT INTO test.configuration_snapshot(asset_id, fingerprint, hardware_manifest, software_manifest,
        parameter_manifest, captured_from, captured_by, effective_from)
    VALUES (v_asset, 'demo-synthetic-v1', '{"source_kind":"synthetic","robot":"simulation"}',
        '{"collector":"open-source-demo"}', '{"motion":"no-hardware"}',
        'synthetic demo', v_actor, clock_timestamp())
    ON CONFLICT(asset_id, fingerprint) DO UPDATE SET captured_from = EXCLUDED.captured_from
    RETURNING id INTO v_configuration;
    INSERT INTO test.test_cycle(cycle_code, campaign_id, asset_id, status, started_at)
    VALUES ('DEMO-SYNTHETIC-001', v_campaign, v_asset, 'ACTIVE', clock_timestamp())
    ON CONFLICT(cycle_code) DO UPDATE SET status = 'ACTIVE' RETURNING id INTO v_cycle;
    INSERT INTO test.analysis_segment(campaign_id, cycle_id, configuration_snapshot_id, segment_no, reason, started_at)
    VALUES (v_campaign, v_cycle, v_configuration, 1, 'ORIGINAL', clock_timestamp())
    ON CONFLICT(cycle_id, segment_no) DO NOTHING;
END
$demo$;
