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
    SELECT * INTO s FROM integration.data_submission WHERE public_id=p_id FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION '提交不存在' USING ERRCODE='P0002'; END IF;
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
        SELECT e.id INTO execution
        FROM test.test_execution e
        JOIN test.runtime_interval ri ON ri.execution_id = e.id AND NOT ri.voided
        WHERE e.campaign_id = ctx.campaign_id
          AND e.cycle_id = ctx.cycle_id
          AND e.asset_id = ctx.asset_id
          AND e.configuration_snapshot_id = ctx.configuration_id
          AND e.test_case_version_id = ctx.version_id
          AND e.station_id = ctx.station_id
          AND e.approved_submission_id IS NULL
          AND e.status <> 'VOIDED'
          AND tstzrange(ri.started_at, ri.ended_at, '[)') &&
              tstzrange((p->>'started_at')::timestamptz, (p->>'ended_at')::timestamptz, '[)')
        ORDER BY ri.started_at
        LIMIT 1
        FOR UPDATE OF e;
        IF execution IS NULL THEN
            INSERT INTO test.test_execution(execution_code,campaign_id,cycle_id,asset_id,configuration_snapshot_id,
                test_case_version_id,station_id,operator_id,status,normalized_started_at,normalized_ended_at,
                code_timestamp,source_time,received_at,clock_quality,data_quality,approved_submission_id)
            VALUES('GENERATED',ctx.campaign_id,ctx.cycle_id,ctx.asset_id,ctx.configuration_id,ctx.version_id,ctx.station_id,
                s.submitter_id,mapped,(p->>'started_at')::timestamptz,(p->>'ended_at')::timestamptz,
                (p->>'started_at')::timestamptz,(p->>'ended_at')::timestamptz,clock_timestamp(),p->>'clock_quality',p->>'data_quality',s.id)
            RETURNING id INTO execution;
            created := true;
        ELSE
            UPDATE test.test_execution
            SET status = mapped,
                approved_submission_id = s.id,
                normalized_ended_at = coalesce(normalized_ended_at, (p->>'ended_at')::timestamptz),
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
        IF created THEN
            INSERT INTO test.runtime_interval(campaign_id,asset_id,cycle_id,execution_id,source_kind,
                started_at,ended_at,active_seconds,clock_quality,data_quality,source_method)
            VALUES(ctx.campaign_id,ctx.asset_id,ctx.cycle_id,execution,'MANUAL',
                (p->>'started_at')::timestamptz,(p->>'ended_at')::timestamptz,(p->>'active_seconds')::numeric,
                p->>'clock_quality',p->>'data_quality','approved-summary-unqualified');
        END IF;
    END IF;
    INSERT INTO integration.data_submission_review(submission_id,revision,reviewer_id,decision,reason,idempotency_key)
    VALUES(s.id,s.revision,actor,p_decision,p_reason,p_key);
    UPDATE integration.data_submission SET status=p_decision,updated_at=clock_timestamp() WHERE id=s.id;
    RETURN s.public_id;
END $$;
