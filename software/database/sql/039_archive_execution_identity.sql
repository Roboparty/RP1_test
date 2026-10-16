-- Stable acquisition identity, actual-station corrections and permanent aliases.
ALTER TABLE test.test_execution ADD COLUMN acquisition_uuid uuid UNIQUE;
CREATE TABLE integration.execution_archive_alias (
    alias text PRIMARY KEY,
    archive_id bigint NOT NULL REFERENCES integration.execution_archive(id),
    execution_id bigint NOT NULL REFERENCES test.test_execution(id),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
ALTER TABLE integration.execution_archive_alias ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON integration.execution_archive_alias FROM PUBLIC,__RP1_APP_USER__;
GRANT SELECT ON integration.execution_archive_alias TO __RP1_APP_USER__;
CREATE POLICY archive_alias_read ON integration.execution_archive_alias FOR SELECT USING (integration.can_read_archive(archive_id));
CREATE TRIGGER archive_alias_immutable BEFORE UPDATE OR DELETE ON integration.execution_archive_alias FOR EACH ROW EXECUTE FUNCTION integration.archive_immutable();
CREATE TRIGGER archive_alias_audit AFTER INSERT OR UPDATE OR DELETE ON integration.execution_archive_alias FOR EACH ROW EXECUTE FUNCTION audit.capture_row_change();
ALTER FUNCTION integration.submission_context(jsonb) RENAME TO planned_submission_context;
CREATE FUNCTION integration.archive_context(p jsonb,p_archive_id bigint)
RETURNS TABLE(campaign_id bigint, cycle_id bigint, segment_id bigint, asset_id bigint,
              configuration_id bigint, version_id bigint, station_id bigint)
LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog, integration, test, catalog AS $$
    SELECT c.id, cy.id, sg.id, a.id, cfg.id, v.id, st.id
    FROM test.test_campaign c
    JOIN test.test_cycle cy ON cy.campaign_id=c.id AND cy.public_id=(p->>'cycle_id')::uuid
    JOIN test.asset a ON a.id=cy.asset_id AND a.public_id=(p->>'asset_id')::uuid AND NOT a.voided
    JOIN test.configuration_snapshot cfg ON cfg.asset_id=a.id AND cfg.public_id=(p->>'configuration_id')::uuid
    JOIN test.analysis_segment sg ON sg.campaign_id=c.id AND sg.cycle_id=cy.id
        AND sg.configuration_snapshot_id=cfg.id AND sg.public_id=(p->>'segment_id')::uuid
    JOIN catalog.test_case_version v ON v.public_id=(p->>'test_case_version_id')::uuid AND v.status='PUBLISHED'
    JOIN catalog.test_case tc ON tc.id=v.test_case_id AND tc.enabled AND tc.asset_kind=a.asset_kind
    JOIN test.station st ON st.public_id=(p->>'station_id')::uuid AND st.enabled
        AND st.station_type IN (a.asset_kind, 'SHARED')
    JOIN test.lab lab ON lab.id=st.lab_id AND lab.enabled
    JOIN test.site site ON site.id=lab.site_id AND site.enabled
    JOIN test.campaign_asset ca ON ca.campaign_id=c.id AND ca.asset_id=a.id AND ca.left_at IS NULL
    JOIN test.campaign_coverage_item ci ON ci.campaign_id=c.id AND ci.target_asset_id=a.id
        AND ci.test_case_version_id=v.id AND ci.status <> 'CANCELLED'
        AND ci.target_configuration_selector @> jsonb_build_object(
            'cycle_id', p->>'cycle_id', 'segment_id', p->>'segment_id',
            'configuration_id', p->>'configuration_id')
    WHERE EXISTS(SELECT 1 FROM integration.execution_archive ar WHERE ar.id=p_archive_id) AND c.public_id=(p->>'campaign_id')::uuid AND c.asset_kind=a.asset_kind
        AND c.status IN ('PLANNED', 'ACTIVE', 'PAUSED') AND cy.status <> 'CANCELLED'
    FOR SHARE OF c, cy, a, cfg, sg, v, tc, st, lab, site, ca, ci;
$$;

CREATE FUNCTION integration.submission_context(p jsonb)
RETURNS TABLE(campaign_id bigint,cycle_id bigint,segment_id bigint,asset_id bigint,configuration_id bigint,version_id bigint,station_id bigint)
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,integration AS $$
DECLARE archive_id bigint;
BEGIN
    SELECT id INTO archive_id FROM integration.execution_archive WHERE execution_uuid=(p->>'execution_uuid')::uuid;
    IF archive_id IS NULL THEN RETURN QUERY SELECT * FROM integration.planned_submission_context(p);
    ELSE RETURN QUERY SELECT * FROM integration.archive_context(p,archive_id); END IF;
END $$;
REVOKE ALL ON FUNCTION integration.archive_context(jsonb,bigint),integration.submission_context(jsonb) FROM PUBLIC;
CREATE OR REPLACE FUNCTION test.sync_live_runs(p_connection_id uuid)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, test, integration, iam, catalog
AS $$
DECLARE
    v_presence integration.hmi_presence%ROWTYPE;
    v_online boolean := false;
    v_run jsonb;
    v_key text;
    v_uuid uuid;
    previous_run test.test_execution%ROWTYPE;
    v_state text;
    v_active numeric(20,3);
    v_campaign bigint;
    v_cycle bigint;
    v_asset bigint;
    v_configuration bigint;
    v_version bigint;
    v_station bigint;
    v_execution test.test_execution%ROWTYPE;
    v_status text;
    v_result jsonb := '[]'::jsonb;
BEGIN
    PERFORM test.expire_stale_live_executions();
    SELECT * INTO v_presence FROM integration.hmi_presence WHERE connection_id = p_connection_id;
    IF NOT FOUND THEN
        RETURN v_result;
    END IF;
    v_online := v_presence.disconnected_at IS NULL
        AND v_presence.last_seen_at > clock_timestamp() - interval '45 seconds';
    IF NOT v_online THEN
        RETURN v_result;
    END IF;

    FOR v_run IN SELECT value FROM jsonb_array_elements(v_presence.runs)
    LOOP
        v_key := left(coalesce(v_run->>'session_key', ''), 128);
        v_state := lower(coalesce(v_run->>'state', ''));
        v_uuid := nullif(v_run->>'execution_uuid','')::uuid;
        v_active := least(315360000, greatest(0, coalesce((v_run->>'active_seconds')::numeric, 0)));
        IF v_key = '' THEN
            CONTINUE;
        END IF;
        IF v_uuid IS NOT NULL THEN PERFORM pg_advisory_xact_lock(hashtextextended(v_uuid::text,38)); END IF;
        IF v_uuid IS NOT NULL AND EXISTS(SELECT 1 FROM test.test_execution WHERE acquisition_uuid=v_uuid AND operator_id IS DISTINCT FROM v_presence.user_id) THEN
            RAISE EXCEPTION '采集身份属于其他操作账号' USING ERRCODE='42501';
        END IF;
        IF v_uuid IS NOT NULL AND EXISTS(SELECT 1 FROM integration.execution_archive WHERE execution_uuid=v_uuid AND owner_id IS DISTINCT FROM v_presence.user_id) THEN
            RAISE EXCEPTION '归档身份属于其他操作账号' USING ERRCODE='42501';
        END IF;

        -- A window can start a new acquisition before the next 10-second heartbeat.
        -- Close the previous online projection; it remains unapproved evidence.
        IF v_state='running' AND v_uuid IS NOT NULL THEN
            FOR previous_run IN SELECT * FROM test.test_execution
                WHERE live_connection_id=p_connection_id AND live_session_key=v_key
                  AND status IN ('RUNNING','PAUSED') AND acquisition_uuid IS NOT NULL
                  AND operator_id=v_presence.user_id AND approved_submission_id IS NULL
                  AND acquisition_uuid<>v_uuid FOR UPDATE
            LOOP
                UPDATE test.test_execution SET status='COMPLETED',normalized_ended_at=clock_timestamp()
                    WHERE id=previous_run.id;
                IF NOT EXISTS(SELECT 1 FROM test.runtime_interval WHERE execution_id=previous_run.id AND NOT voided) THEN
                    INSERT INTO test.runtime_interval(campaign_id,asset_id,cycle_id,execution_id,source_kind,started_at,ended_at,active_seconds,clock_quality,data_quality,source_method)
                    VALUES(previous_run.campaign_id,previous_run.asset_id,previous_run.cycle_id,previous_run.id,'NATIVE',previous_run.normalized_started_at,
                        previous_run.normalized_started_at+make_interval(secs=>previous_run.reported_active_seconds),previous_run.reported_active_seconds,'VALID','PARTIAL','hmi-live-heartbeat');
                END IF;
            END LOOP;
        END IF;
        SELECT * INTO v_execution
        FROM test.test_execution
        WHERE (v_uuid IS NOT NULL AND acquisition_uuid=v_uuid AND operator_id=v_presence.user_id)
           OR (live_connection_id=p_connection_id AND live_session_key=v_key AND (v_uuid IS NULL OR acquisition_uuid IS NULL))
        ORDER BY id DESC LIMIT 1 FOR UPDATE;
        IF v_execution.approved_submission_id IS NOT NULL AND v_execution.acquisition_uuid=v_uuid THEN
            v_result:=v_result||jsonb_build_array(jsonb_build_object('session_key',v_key,'execution_code',v_execution.execution_code,'execution_id',v_execution.public_id,'status',v_execution.status));
            v_execution:=NULL;
            CONTINUE;
        END IF;

        v_campaign := NULL;
        IF v_state = 'running'
           AND coalesce(v_run->>'campaign_id', '') ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
           AND coalesce(v_run->>'cycle_id', '') ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
           AND coalesce(v_run->>'asset_id', '') ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
           AND coalesce(v_run->>'configuration_id', '') ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
           AND coalesce(v_run->>'test_case_version_id', '') ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
           AND coalesce(v_run->>'station_id', '') ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
        THEN
            SELECT c.id, cy.id, a.id, cfg.id, ver.id, s.id
            INTO v_campaign, v_cycle, v_asset, v_configuration, v_version, v_station
            FROM test.test_campaign c
            JOIN test.test_cycle cy ON cy.public_id = (v_run->>'cycle_id')::uuid AND cy.campaign_id = c.id
            JOIN test.asset a ON a.public_id = (v_run->>'asset_id')::uuid
            JOIN test.configuration_snapshot cfg ON cfg.public_id = (v_run->>'configuration_id')::uuid AND cfg.asset_id = a.id
            JOIN catalog.test_case_version ver ON ver.public_id = (v_run->>'test_case_version_id')::uuid
            JOIN test.station s ON s.public_id = (v_run->>'station_id')::uuid
            WHERE c.public_id = (v_run->>'campaign_id')::uuid;
        END IF;

        IF v_state = 'running' AND v_campaign IS NOT NULL THEN
            IF v_execution.acquisition_uuid=v_uuid AND ROW(v_execution.campaign_id,v_execution.cycle_id,v_execution.asset_id,v_execution.configuration_snapshot_id,v_execution.test_case_version_id)
                IS DISTINCT FROM ROW(v_campaign,v_cycle,v_asset,v_configuration,v_version) THEN
                RAISE EXCEPTION '采集身份与执行上下文不一致' USING ERRCODE='22023';
            END IF;
            IF v_execution.id IS NOT NULL AND (v_execution.status IN ('RUNNING', 'PAUSED') OR v_execution.acquisition_uuid=v_uuid) THEN
                IF v_execution.status NOT IN ('RUNNING','PAUSED') THEN
                    UPDATE test.runtime_interval SET voided=true
                    WHERE execution_id=v_execution.id AND source_method='hmi-live-heartbeat' AND NOT voided;
                END IF;
                UPDATE test.test_execution
                SET status = 'RUNNING',
                    normalized_ended_at=NULL,
                    acquisition_uuid=coalesce(acquisition_uuid,v_uuid),
                    live_connection_id=p_connection_id,live_session_key=v_key,
                    reported_active_seconds = v_active,
                    received_at = clock_timestamp(),
                    operator_id = coalesce(operator_id, v_presence.user_id)
                WHERE id = v_execution.id
                RETURNING * INTO v_execution;
            ELSE
                INSERT INTO test.test_execution(
                    execution_code, campaign_id, cycle_id, asset_id, configuration_snapshot_id,
                    test_case_version_id, station_id, operator_id, status,
                    normalized_started_at, code_timestamp, source_time, received_at,
                    clock_quality, data_quality, live_connection_id, live_session_key,
                    reported_active_seconds, acquisition_uuid
                ) VALUES (
                    'GENERATED', v_campaign, v_cycle, v_asset, v_configuration,
                    v_version, v_station, v_presence.user_id, 'RUNNING',
                    clock_timestamp(), clock_timestamp(), clock_timestamp(), clock_timestamp(),
                    'VALID', 'PARTIAL', p_connection_id, v_key, v_active, v_uuid
                )
                RETURNING * INTO v_execution;
            END IF;
        ELSIF v_execution.id IS NOT NULL AND v_execution.status = 'RUNNING' THEN
            v_status := CASE v_state
                WHEN 'paused' THEN 'PAUSED'
                WHEN 'fault' THEN 'FAILED'
                ELSE 'COMPLETED'
            END;
            UPDATE test.test_execution
            SET status = v_status,
                reported_active_seconds = v_active,
                received_at = clock_timestamp(),
                normalized_ended_at = CASE WHEN v_status = 'PAUSED' THEN NULL ELSE clock_timestamp() END
            WHERE id = v_execution.id
            RETURNING * INTO v_execution;
            IF v_status IN ('COMPLETED', 'FAILED')
               AND v_execution.normalized_started_at IS NOT NULL
               AND NOT EXISTS (
                   SELECT 1 FROM test.runtime_interval ri
                   WHERE ri.execution_id = v_execution.id AND NOT ri.voided
               )
            THEN
                INSERT INTO test.runtime_interval(
                    campaign_id, asset_id, cycle_id, execution_id, source_kind,
                    started_at, ended_at, active_seconds, clock_quality, data_quality, source_method
                ) VALUES (
                    v_execution.campaign_id, v_execution.asset_id, v_execution.cycle_id, v_execution.id,
                    'NATIVE', v_execution.normalized_started_at,
                    v_execution.normalized_started_at + make_interval(secs => v_execution.reported_active_seconds),
                    v_execution.reported_active_seconds, 'VALID', 'PARTIAL', 'hmi-live-heartbeat'
                );
            END IF;
        END IF;

        IF v_execution.id IS NOT NULL AND v_execution.live_session_key = v_key THEN
            v_result := v_result || jsonb_build_array(jsonb_build_object(
                'session_key', v_key,
                'execution_code', v_execution.execution_code,
                'execution_id', v_execution.public_id,
                'status', v_execution.status
            ));
        END IF;
        v_execution := NULL;
        v_campaign := NULL;
    END LOOP;
    RETURN v_result;
END
$$;

-- Approving a summary for a run that already has a live runtime interval
-- attaches the decision to that execution instead of inserting a second overlap.

CREATE OR REPLACE FUNCTION integration.review_test_data(p_id uuid, p_revision integer, p_decision text, p_reason text, p_key text)
RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, integration, iam, test AS $$
DECLARE
    actor bigint := iam.current_user_id();
    s integration.data_submission%ROWTYPE;
    r integration.data_submission_review%ROWTYPE;
    p jsonb;
    ctx record;
    execution bigint;
    mapped text;
    created boolean := false;
    original_code text;
    archive_id bigint;
BEGIN
    IF iam.current_principal_kind() <> 'HUMAN' OR NOT iam.can_write_business() THEN
        RAISE EXCEPTION '需要执行员或管理员身份' USING ERRCODE='42501'; END IF;
    IF p_id IS NULL OR p_revision IS NULL OR p_decision IS NULL OR p_reason IS NULL OR p_key IS NULL
        OR p_decision NOT IN ('APPROVED','REJECTED') OR p_revision<1
        OR length(btrim(p_reason)) NOT BETWEEN 1 AND 300 OR length(p_key) NOT BETWEEN 8 AND 200 THEN
        RAISE EXCEPTION '审核参数无效' USING ERRCODE='22023'; END IF;
    PERFORM pg_advisory_xact_lock(hashtextextended(actor::text || ':' || p_key,2));
    SELECT * INTO r FROM integration.data_submission_review WHERE reviewer_id=actor AND idempotency_key=p_key;
    IF FOUND THEN
        SELECT * INTO s FROM integration.data_submission WHERE id=r.submission_id;
        IF s.public_id<>p_id OR r.revision<>p_revision OR r.decision<>p_decision OR r.reason<>p_reason THEN
            RAISE EXCEPTION '审核幂等键内容冲突' USING ERRCODE='P0001'; END IF;
        RETURN s.public_id;
    END IF;
    SELECT * INTO s FROM integration.data_submission WHERE public_id=p_id;
    IF NOT FOUND THEN RAISE EXCEPTION '提交不存在' USING ERRCODE='P0002'; END IF;
    -- Match submit/edit lock order: archive first, then submission.
    PERFORM pg_advisory_xact_lock(hashtextextended(s.execution_uuid::text,38));
    PERFORM 1 FROM integration.execution_archive WHERE execution_uuid=s.execution_uuid FOR UPDATE;
    SELECT * INTO s FROM integration.data_submission WHERE public_id=p_id FOR UPDATE;
    IF s.status<>'PENDING' OR s.revision<>p_revision THEN
        RAISE EXCEPTION '审核版本已变化或已有审核结果，请刷新' USING ERRCODE='P0001'; END IF;
    SELECT payload INTO p FROM integration.data_submission_revision WHERE submission_id=s.id AND revision=s.revision;
    PERFORM set_config('app.change_reason',p_reason,true);
    IF p_decision='APPROVED' THEN
        SELECT * INTO ctx FROM integration.submission_context(p);
        IF NOT FOUND THEN RAISE EXCEPTION '执行上下文已失效，不能批准' USING ERRCODE='22023'; END IF;
        IF NOT EXISTS (SELECT 1 FROM iam.app_user u WHERE u.id=s.submitter_id AND u.enabled
            AND u.principal_kind='HUMAN' AND u.role IN ('SYSTEM_ADMIN', 'TEST_EXECUTOR')) THEN
            RAISE EXCEPTION '提交人账号已停用，不能批准' USING ERRCODE='42501'; END IF;
        mapped := CASE p->>'source_status' WHEN 'passed' THEN 'COMPLETED' WHEN 'failed' THEN 'FAILED' ELSE 'BLOCKED' END;
        SELECT e.id,e.execution_code INTO execution,original_code
        FROM test.test_execution e
        LEFT JOIN test.runtime_interval ri ON ri.execution_id = e.id AND NOT ri.voided
        WHERE e.campaign_id = ctx.campaign_id
          AND e.cycle_id = ctx.cycle_id
          AND e.asset_id = ctx.asset_id
          AND e.configuration_snapshot_id = ctx.configuration_id
          AND e.test_case_version_id = ctx.version_id
          AND e.approved_submission_id IS NULL
          AND e.operator_id=s.submitter_id
          AND e.status <> 'VOIDED'
          AND (e.acquisition_uuid=(p->>'execution_uuid')::uuid OR
              (e.acquisition_uuid IS NULL AND e.live_connection_id IS NOT NULL
               AND tstzrange(ri.started_at,ri.ended_at,'[)') && tstzrange((p->>'started_at')::timestamptz,(p->>'ended_at')::timestamptz,'[)')))
        ORDER BY ri.started_at
        LIMIT 1
        FOR UPDATE OF e;
        IF execution IS NULL THEN
            INSERT INTO test.test_execution(execution_code,campaign_id,cycle_id,asset_id,configuration_snapshot_id,
                test_case_version_id,station_id,operator_id,status,normalized_started_at,normalized_ended_at,
                code_timestamp,source_time,received_at,clock_quality,data_quality,approved_submission_id,acquisition_uuid)
            VALUES('GENERATED',ctx.campaign_id,ctx.cycle_id,ctx.asset_id,ctx.configuration_id,ctx.version_id,ctx.station_id,
                s.submitter_id,mapped,(p->>'started_at')::timestamptz,(p->>'ended_at')::timestamptz,
                (p->>'started_at')::timestamptz,(p->>'ended_at')::timestamptz,clock_timestamp(),p->>'clock_quality',p->>'data_quality',s.id,(p->>'execution_uuid')::uuid)
            RETURNING id INTO execution;
            created := true;
        ELSE
            UPDATE test.test_execution
            SET status = mapped,
                station_id=ctx.station_id,
                approved_submission_id = s.id,
                acquisition_uuid=(p->>'execution_uuid')::uuid,
                normalized_started_at=(p->>'started_at')::timestamptz,
                normalized_ended_at=(p->>'ended_at')::timestamptz,
                data_quality = p->>'data_quality',
                clock_quality = p->>'clock_quality',
                operator_id = coalesce(operator_id, s.submitter_id),
                received_at = clock_timestamp()
            WHERE id = execution;
        END IF;
        INSERT INTO test.execution_stage(campaign_id,execution_id,stage_code,stage_name,sequence_no,status,
            normalized_started_at,normalized_ended_at,clock_quality)
        SELECT ctx.campaign_id, execution, 'APPROVED_SUMMARY', '审核通过的测试摘要', 1, mapped,
            (p->>'started_at')::timestamptz, (p->>'ended_at')::timestamptz, p->>'clock_quality'
        WHERE NOT EXISTS (
            SELECT 1 FROM test.execution_stage stage
            WHERE stage.execution_id = execution AND stage.sequence_no = 1
        );
        INSERT INTO test.execution_result(execution_id,source_status,outcome,termination_kind,summary,
            exception_count,completed_cycles,reported_duration_seconds,executor_display,archive_status,source_created_at,source_updated_at)
        VALUES(execution,p->>'source_status',CASE p->>'source_status' WHEN 'passed' THEN 'PASSED' WHEN 'failed' THEN 'FAILED' ELSE 'INCONCLUSIVE' END,
            CASE p->>'source_status' WHEN 'blocked' THEN 'UNKNOWN' ELSE 'NORMAL' END,p->>'summary',
            (p->>'exception_count')::integer,(p->>'completed_cycles')::integer,(p->>'active_seconds')::numeric,
            (SELECT display_name FROM iam.app_user WHERE id=s.submitter_id),'MISSING',
            (p->>'started_at')::timestamptz,(p->>'ended_at')::timestamptz)
        ON CONFLICT (execution_id) DO UPDATE SET
            source_status = EXCLUDED.source_status,
            outcome = EXCLUDED.outcome,
            termination_kind = EXCLUDED.termination_kind,
            summary = EXCLUDED.summary,
            exception_count = EXCLUDED.exception_count,
            completed_cycles = EXCLUDED.completed_cycles,
            reported_duration_seconds = EXCLUDED.reported_duration_seconds,
            executor_display = EXCLUDED.executor_display,
            source_updated_at = EXCLUDED.source_updated_at,
            updated_at = clock_timestamp();
        SELECT id INTO STRICT archive_id FROM integration.execution_archive WHERE execution_uuid=(p->>'execution_uuid')::uuid;
        INSERT INTO integration.execution_archive_alias(alias,archive_id,execution_id)
        SELECT code,archive_id,execution FROM (SELECT original_code AS code UNION SELECT execution_code FROM test.test_execution WHERE id=execution) codes
        WHERE code IS NOT NULL ON CONFLICT(alias) DO NOTHING;
        IF created OR NOT EXISTS(SELECT 1 FROM test.runtime_interval WHERE execution_id=execution AND NOT voided) THEN
            INSERT INTO test.runtime_interval(campaign_id,asset_id,cycle_id,execution_id,source_kind,
                started_at,ended_at,active_seconds,clock_quality,data_quality,source_method)
            VALUES(ctx.campaign_id,ctx.asset_id,ctx.cycle_id,execution,'MANUAL',
                (p->>'started_at')::timestamptz,(p->>'ended_at')::timestamptz,(p->>'active_seconds')::numeric,
                p->>'clock_quality',p->>'data_quality','approved-summary-unqualified');
        ELSE
            UPDATE test.runtime_interval SET started_at=(p->>'started_at')::timestamptz,ended_at=(p->>'ended_at')::timestamptz,
                active_seconds=(p->>'active_seconds')::numeric,clock_quality=p->>'clock_quality',data_quality=p->>'data_quality',
                source_kind='MANUAL',source_method='approved-summary-unqualified'
            WHERE execution_id=execution AND NOT voided;
        END IF;
    END IF;
    INSERT INTO integration.data_submission_review(submission_id,revision,reviewer_id,decision,reason,idempotency_key)
    VALUES(s.id,s.revision,actor,p_decision,p_reason,p_key);
    UPDATE integration.data_submission SET status=p_decision,updated_at=clock_timestamp() WHERE id=s.id;
    RETURN s.public_id;
END $$;
