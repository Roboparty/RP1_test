-- Executors and administrators can read every summary, including their own,
-- and can review it. A valid planned context is still required. Viewers stay out.
-- Pending and rejected rows still do not enter formal statistics.

DROP POLICY submission_read ON integration.data_submission;
CREATE POLICY submission_read ON integration.data_submission FOR SELECT USING (
    iam.current_principal_kind() = 'HUMAN'
    AND iam.current_user_role() IN ('SYSTEM_ADMIN', 'TEST_EXECUTOR')
);

CREATE OR REPLACE FUNCTION integration.submit_test_data(p jsonb, p_expected integer, p_key text, p_reason text)
RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, integration, iam, public AS $$
DECLARE
    actor bigint := iam.current_user_id();
    ctx record;
    s integration.data_submission%ROWTYPE;
    receipt integration.data_submission_revision%ROWTYPE;
    digest text := encode(public.digest(convert_to(jsonb_build_object('payload',p,'expected',p_expected,'reason',p_reason)::text,'UTF8'),'sha256'),'hex');
BEGIN
    IF iam.current_principal_kind() <> 'HUMAN' OR NOT iam.can_write_business() THEN
        RAISE EXCEPTION '需要执行员或管理员身份' USING ERRCODE='42501';
    END IF;
    IF p IS NULL OR p_expected IS NULL OR p_key IS NULL OR p_reason IS NULL
        OR jsonb_typeof(p) <> 'object' OR octet_length(p::text)>8192
        OR length(p_key) NOT BETWEEN 8 AND 200 OR length(btrim(p_reason)) NOT BETWEEN 3 AND 300
        OR p_expected < 0 THEN RAISE EXCEPTION '提交参数无效' USING ERRCODE='22023'; END IF;
    IF (SELECT count(*) FROM jsonb_object_keys(p)) <> 18 OR EXISTS (SELECT 1 FROM jsonb_each(p) e WHERE e.value='null'::jsonb) OR EXISTS (
        SELECT 1 FROM jsonb_object_keys(p) k WHERE k <> ALL(ARRAY[
            'execution_uuid','campaign_id','cycle_id','segment_id','asset_id','configuration_id',
            'test_case_version_id','station_id','started_at','ended_at','active_seconds','completed_cycles',
            'source_status','data_quality','clock_quality','exception_count','summary','evidence_sha256'])
    ) THEN RAISE EXCEPTION '只接受有界测试摘要，禁止附件与时序数据' USING ERRCODE='22023'; END IF;
    PERFORM pg_advisory_xact_lock(hashtextextended(actor::text || ':' || p_key,0));
    SELECT * INTO receipt FROM integration.data_submission_revision WHERE submitter_id=actor AND idempotency_key=p_key;
    IF FOUND THEN
        IF receipt.request_hash <> digest THEN RAISE EXCEPTION '幂等键内容冲突' USING ERRCODE='P0001'; END IF;
        SELECT * INTO s FROM integration.data_submission WHERE id=receipt.submission_id;
        RETURN s.public_id;
    END IF;
    IF p->>'source_status' NOT IN ('passed','failed','blocked')
        OR p->>'data_quality' NOT IN ('VALID','PARTIAL','INVALID','MISSING')
        OR p->>'clock_quality' NOT IN ('VALID','PARTIAL','INVALID')
        OR (p->>'evidence_sha256') !~ '^[0-9a-f]{64}$' OR length(p->>'summary') > 500
        OR (p->>'active_seconds')::numeric NOT BETWEEN 0 AND 315360000
        OR (p->>'completed_cycles')::bigint NOT BETWEEN 0 AND 2147483647
        OR (p->>'exception_count')::bigint NOT BETWEEN 0 AND 2147483647
        OR NOT isfinite((p->>'started_at')::timestamptz) OR NOT isfinite((p->>'ended_at')::timestamptz)
        OR (p->>'ended_at')::timestamptz < (p->>'started_at')::timestamptz
        OR (p->>'ended_at')::timestamptz > clock_timestamp()+interval '5 minutes'
        OR (p->>'active_seconds')::numeric > extract(epoch FROM ((p->>'ended_at')::timestamptz-(p->>'started_at')::timestamptz))+.001
    THEN RAISE EXCEPTION '摘要状态、时长或证据摘要无效' USING ERRCODE='22023'; END IF;
    SELECT * INTO ctx FROM integration.submission_context(p);
    IF NOT FOUND THEN RAISE EXCEPTION '执行上下文无效或已停用' USING ERRCODE='22023'; END IF;
    PERFORM pg_advisory_xact_lock(hashtextextended(p->>'execution_uuid',1));
    SELECT * INTO s FROM integration.data_submission WHERE execution_uuid=(p->>'execution_uuid')::uuid FOR UPDATE;
    PERFORM set_config('app.change_reason',p_reason,true);
    IF s.id IS NULL THEN
        IF p_expected <> 0 THEN RAISE EXCEPTION '提交版本冲突' USING ERRCODE='P0001'; END IF;
        INSERT INTO integration.data_submission(execution_uuid,submitter_id,campaign_id,revision,status)
        VALUES((p->>'execution_uuid')::uuid,actor,ctx.campaign_id,1,'PENDING') RETURNING * INTO s;
    ELSE
        IF s.submitter_id <> actor THEN RAISE EXCEPTION '不能提交他人的记录' USING ERRCODE='42501'; END IF;
        IF s.campaign_id <> ctx.campaign_id OR s.revision <> p_expected OR s.status <> 'REJECTED' THEN
            RAISE EXCEPTION '仅驳回的当前版本可重提，已批准记录不可改写' USING ERRCODE='P0001'; END IF;
        UPDATE integration.data_submission SET revision=revision+1,status='PENDING',updated_at=clock_timestamp()
        WHERE id=s.id RETURNING * INTO s;
    END IF;
    INSERT INTO integration.data_submission_revision(submission_id,revision,submitter_id,idempotency_key,request_hash,payload,reason)
    VALUES(s.id,s.revision,actor,p_key,digest,p,p_reason);
    RETURN s.public_id;
END $$;

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
BEGIN
    IF iam.current_principal_kind() <> 'HUMAN' OR NOT iam.can_write_business() THEN
        RAISE EXCEPTION '需要执行员或管理员身份' USING ERRCODE='42501'; END IF;
    IF p_id IS NULL OR p_revision IS NULL OR p_decision IS NULL OR p_reason IS NULL OR p_key IS NULL
        OR p_decision NOT IN ('APPROVED','REJECTED') OR p_revision<1
        OR length(btrim(p_reason)) NOT BETWEEN 3 AND 300 OR length(p_key) NOT BETWEEN 8 AND 200 THEN
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
        INSERT INTO test.test_execution(execution_code,campaign_id,cycle_id,asset_id,configuration_snapshot_id,
            test_case_version_id,station_id,operator_id,status,normalized_started_at,normalized_ended_at,
            code_timestamp,source_time,received_at,clock_quality,data_quality,approved_submission_id)
        VALUES('GENERATED',ctx.campaign_id,ctx.cycle_id,ctx.asset_id,ctx.configuration_id,ctx.version_id,ctx.station_id,
            s.submitter_id,mapped,(p->>'started_at')::timestamptz,(p->>'ended_at')::timestamptz,
            (p->>'started_at')::timestamptz,(p->>'ended_at')::timestamptz,clock_timestamp(),p->>'clock_quality',p->>'data_quality',s.id)
        RETURNING id INTO execution;
        INSERT INTO test.execution_stage(campaign_id,execution_id,stage_code,stage_name,sequence_no,status,
            normalized_started_at,normalized_ended_at,clock_quality)
        VALUES(ctx.campaign_id,execution,'APPROVED_SUMMARY','审核通过的测试摘要',1,mapped,
            (p->>'started_at')::timestamptz,(p->>'ended_at')::timestamptz,p->>'clock_quality');
        INSERT INTO test.execution_result(execution_id,source_status,outcome,termination_kind,summary,
            exception_count,completed_cycles,reported_duration_seconds,executor_display,archive_status,source_created_at,source_updated_at)
        VALUES(execution,p->>'source_status',CASE p->>'source_status' WHEN 'passed' THEN 'PASSED' WHEN 'failed' THEN 'FAILED' ELSE 'INCONCLUSIVE' END,
            CASE p->>'source_status' WHEN 'blocked' THEN 'UNKNOWN' ELSE 'NORMAL' END,p->>'summary',
            (p->>'exception_count')::integer,(p->>'completed_cycles')::integer,(p->>'active_seconds')::numeric,
            (SELECT display_name FROM iam.app_user WHERE id=s.submitter_id),'MISSING',
            (p->>'started_at')::timestamptz,(p->>'ended_at')::timestamptz);
        INSERT INTO test.runtime_interval(campaign_id,asset_id,cycle_id,execution_id,source_kind,
            started_at,ended_at,active_seconds,clock_quality,data_quality,source_method)
        VALUES(ctx.campaign_id,ctx.asset_id,ctx.cycle_id,execution,'MANUAL',
            (p->>'started_at')::timestamptz,(p->>'ended_at')::timestamptz,(p->>'active_seconds')::numeric,
            p->>'clock_quality',p->>'data_quality','approved-summary-unqualified');
    END IF;
    INSERT INTO integration.data_submission_review(submission_id,revision,reviewer_id,decision,reason,idempotency_key)
    VALUES(s.id,s.revision,actor,p_decision,p_reason,p_key);
    UPDATE integration.data_submission SET status=p_decision,updated_at=clock_timestamp() WHERE id=s.id;
    RETURN s.public_id;
END $$;
