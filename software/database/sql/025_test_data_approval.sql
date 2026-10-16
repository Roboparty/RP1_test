-- Pending/rejected revisions live outside every existing formal read model.
CREATE TABLE integration.data_submission (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    public_id uuid NOT NULL DEFAULT public.uuid_v7() UNIQUE,
    execution_uuid uuid NOT NULL UNIQUE,
    submitter_id bigint NOT NULL REFERENCES iam.app_user(id),
    campaign_id bigint NOT NULL REFERENCES test.test_campaign(id),
    revision integer NOT NULL CHECK (revision > 0),
    status text NOT NULL CHECK (status IN ('PENDING', 'APPROVED', 'REJECTED')),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE integration.data_submission_revision (
    submission_id bigint NOT NULL REFERENCES integration.data_submission(id),
    revision integer NOT NULL CHECK (revision > 0),
    submitter_id bigint NOT NULL REFERENCES iam.app_user(id),
    idempotency_key text NOT NULL CHECK (length(idempotency_key) BETWEEN 8 AND 200),
    request_hash text NOT NULL,
    payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object' AND octet_length(payload::text) <= 8192),
    reason text NOT NULL CHECK (length(btrim(reason)) BETWEEN 3 AND 300),
    submitted_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY(submission_id, revision),
    UNIQUE(submitter_id, idempotency_key)
);
CREATE TABLE integration.data_submission_review (
    submission_id bigint NOT NULL,
    revision integer NOT NULL,
    reviewer_id bigint NOT NULL REFERENCES iam.app_user(id),
    decision text NOT NULL CHECK (decision IN ('APPROVED', 'REJECTED')),
    reason text NOT NULL CHECK (length(btrim(reason)) BETWEEN 3 AND 300),
    idempotency_key text NOT NULL CHECK (length(idempotency_key) BETWEEN 8 AND 200),
    reviewed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY(submission_id, revision),
    UNIQUE(reviewer_id, idempotency_key),
    FOREIGN KEY(submission_id, revision) REFERENCES integration.data_submission_revision(submission_id, revision)
);
CREATE INDEX ix_data_submission_queue ON integration.data_submission(status, id DESC);
ALTER TABLE test.test_execution ADD COLUMN approved_submission_id bigint UNIQUE REFERENCES integration.data_submission(id);
ALTER TABLE test.execution_result ADD COLUMN completed_cycles integer CHECK (completed_cycles >= 0);

DO $$
DECLARE t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['data_submission', 'data_submission_revision', 'data_submission_review'] LOOP
        EXECUTE format('ALTER TABLE integration.%I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('REVOKE ALL ON integration.%I FROM PUBLIC, __RP1_APP_USER__', t);
        EXECUTE format('GRANT SELECT ON integration.%I TO __RP1_APP_USER__', t);
        EXECUTE format('CREATE TRIGGER trg_approval_audit AFTER INSERT OR UPDATE OR DELETE ON integration.%I FOR EACH ROW EXECUTE FUNCTION audit.capture_row_change()', t);
    END LOOP;
END $$;
CREATE POLICY submission_read ON integration.data_submission FOR SELECT USING (
    iam.current_principal_kind() = 'HUMAN' AND
    (iam.is_system_admin() OR (submitter_id = iam.current_user_id() AND iam.can_access_campaign(campaign_id, false)))
);
CREATE POLICY revision_read ON integration.data_submission_revision FOR SELECT USING (
    EXISTS (SELECT 1 FROM integration.data_submission s WHERE s.id = submission_id)
);
CREATE POLICY review_read ON integration.data_submission_review FOR SELECT USING (
    EXISTS (SELECT 1 FROM integration.data_submission s WHERE s.id = submission_id)
);

-- Revalidate the real planned context at submission AND approval time.
CREATE FUNCTION integration.submission_context(p jsonb)
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
            'configuration_id', p->>'configuration_id', 'station_id', p->>'station_id')
    WHERE c.public_id=(p->>'campaign_id')::uuid AND c.asset_kind=a.asset_kind
        AND c.status IN ('PLANNED', 'ACTIVE', 'PAUSED') AND cy.status <> 'CANCELLED'
    FOR SHARE OF c, cy, a, cfg, sg, v, tc, st, lab, site, ca, ci;
$$;

CREATE FUNCTION integration.submit_test_data(p jsonb, p_expected integer, p_key text, p_reason text)
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
        IF NOT iam.can_access_campaign(s.campaign_id,true) THEN RAISE EXCEPTION '计划权限已撤销' USING ERRCODE='42501'; END IF;
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
    IF NOT iam.can_access_campaign(ctx.campaign_id,true) OR NOT iam.can_access_asset(ctx.asset_id,true) THEN
        RAISE EXCEPTION '需要计划及样品EDIT权限' USING ERRCODE='42501';
    END IF;
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
            RAISE EXCEPTION '仅驳回的当前版本可重提，已批准记录不可改写' USING ERRCODE='P0001';
        END IF;
        UPDATE integration.data_submission SET revision=revision+1,status='PENDING',updated_at=clock_timestamp()
        WHERE id=s.id RETURNING * INTO s;
    END IF;
    INSERT INTO integration.data_submission_revision(submission_id,revision,submitter_id,idempotency_key,request_hash,payload,reason)
    VALUES(s.id,s.revision,actor,p_key,digest,p,p_reason);
    RETURN s.public_id;
END $$;

CREATE FUNCTION integration.review_test_data(p_id uuid, p_revision integer, p_decision text, p_reason text, p_key text)
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
    IF iam.current_principal_kind() <> 'HUMAN' OR NOT iam.is_system_admin() THEN
        RAISE EXCEPTION '需要系统管理员审核权限' USING ERRCODE='42501'; END IF;
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
    IF s.submitter_id=actor THEN RAISE EXCEPTION '禁止审核本人提交的数据' USING ERRCODE='42501'; END IF;
    IF s.status<>'PENDING' OR s.revision<>p_revision THEN
        RAISE EXCEPTION '审核版本已变化或已有审核结果，请刷新' USING ERRCODE='P0001'; END IF;
    SELECT payload INTO p FROM integration.data_submission_revision WHERE submission_id=s.id AND revision=s.revision;
    PERFORM set_config('app.change_reason',p_reason,true);
    IF p_decision='APPROVED' THEN
        SELECT * INTO ctx FROM integration.submission_context(p);
        IF NOT FOUND THEN RAISE EXCEPTION '执行上下文已失效，不能批准' USING ERRCODE='22023'; END IF;
        IF NOT EXISTS (SELECT 1 FROM iam.app_user u WHERE u.id=s.submitter_id AND u.enabled
            AND u.principal_kind='HUMAN' AND (u.role='SYSTEM_ADMIN' OR (u.role='TEST_EXECUTOR'
            AND EXISTS(SELECT 1 FROM iam.user_campaign_access ca WHERE ca.user_id=u.id AND ca.campaign_id=s.campaign_id AND ca.access_level='EDIT')))) THEN
            RAISE EXCEPTION '提交人权限已撤销，不能批准' USING ERRCODE='42501'; END IF;
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
        -- Reported aggregate time is official after approval, but remains unassessed for MTBF.
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

-- Application connections cannot write formal acquisition facts directly. The
-- narrowly authorized SECURITY DEFINER approval function is the publication path.
REVOKE INSERT, UPDATE, DELETE ON test.test_execution, test.execution_stage,
    test.execution_result, test.runtime_interval, test.test_event,
    health.metric_series, health.metric_observation, integration.artifact FROM __RP1_APP_USER__;
REVOKE ALL ON FUNCTION integration.submission_context(jsonb) FROM PUBLIC, __RP1_APP_USER__;
REVOKE ALL ON FUNCTION integration.submit_test_data(jsonb,integer,text,text) FROM PUBLIC;
REVOKE ALL ON FUNCTION integration.review_test_data(uuid,integer,text,text,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION integration.submit_test_data(jsonb,integer,text,text) TO __RP1_APP_USER__;
GRANT EXECUTE ON FUNCTION integration.review_test_data(uuid,integer,text,text,text) TO __RP1_APP_USER__;
