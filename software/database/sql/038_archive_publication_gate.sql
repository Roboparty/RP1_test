-- Every new publication is bound to immutable, centrally archived evidence.
-- Existing approved history is left intact; new revisions cannot bypass this gate.
CREATE FUNCTION integration.archive_summary(p_archive_id bigint)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, integration, iam AS $$
DECLARE a integration.execution_archive%ROWTYPE; v jsonb; c jsonb; starts timestamptz; ends timestamptz;
BEGIN
    IF iam.current_principal_kind() <> 'HUMAN' OR NOT iam.can_write_business() THEN
        RAISE EXCEPTION '需要执行员或管理员身份' USING ERRCODE='42501';
    END IF;
    SELECT * INTO a FROM integration.execution_archive WHERE id=p_archive_id FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION '归档不存在' USING ERRCODE='P0002'; END IF;
    IF a.upload_state <> 'COMPLETE' OR a.processing_state <> 'READY'
       OR a.verification_revision IS NULL OR a.classification_revision IS NULL THEN
        RAISE EXCEPTION '归档校验、核实或用例归类尚未完成' USING ERRCODE='22023';
    END IF;
    IF a.manifest->>'original_sha256' IS DISTINCT FROM a.sha256 THEN
        RAISE EXCEPTION '归档证据哈希尚未校验' USING ERRCODE='22023';
    END IF;
    SELECT payload INTO STRICT v FROM integration.archive_verification
        WHERE archive_id=a.id AND revision=a.verification_revision;
    SELECT payload INTO STRICT c FROM integration.archive_classification
        WHERE archive_id=a.id AND revision=a.classification_revision;
    IF v->>'station_id' IS DISTINCT FROM c->>'station_id' THEN
        RAISE EXCEPTION '核实工位与正式执行上下文不一致' USING ERRCODE='22023';
    END IF;
    starts := coalesce((a.facts->>'started_at')::timestamptz, (v->>'declared_started_at')::timestamptz);
    ends := coalesce((a.facts->>'ended_at')::timestamptz, (v->>'declared_ended_at')::timestamptz);
    IF starts IS NULL OR ends IS NULL THEN
        RAISE EXCEPTION '历史记录需补充申报开始与结束时间，不能以文件时间代替' USING ERRCODE='22023';
    END IF;
    RETURN c || jsonb_build_object(
        'execution_uuid',a.execution_uuid,'started_at',starts,'ended_at',ends,
        'active_seconds',a.facts->'active_seconds','completed_cycles',a.facts->'completed_cycles',
        'source_status',CASE a.facts->>'machine_status' WHEN 'completed' THEN 'passed' WHEN 'failed' THEN 'failed' ELSE 'blocked' END,
        'data_quality',CASE
            WHEN a.facts->>'data_quality'='MISSING' OR a.manifest->>'quality_status'='MISSING' THEN 'MISSING'
            WHEN a.facts->>'data_quality'='INVALID' OR a.manifest->>'quality_status'='INVALID' THEN 'INVALID'
            WHEN a.facts->>'data_quality'='PARTIAL' OR a.manifest->>'quality_status'='PARTIAL' THEN 'PARTIAL'
            ELSE 'VALID' END,
        'clock_quality',CASE WHEN a.facts->>'started_at' IS NULL OR a.facts->>'ended_at' IS NULL
            OR a.facts->>'clock_quality' NOT IN ('VALID','PARTIAL') THEN 'INVALID' ELSE a.facts->>'clock_quality' END,
        'exception_count',a.facts->'exception_count','summary',left(coalesce(a.facts->>'summary',''),500),
        'evidence_sha256',a.sha256);
END $$;
REVOKE ALL ON FUNCTION integration.archive_summary(bigint) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION integration.archive_summary(bigint) TO __RP1_APP_USER__;

-- Lock order shared by direct summaries, archive submissions and reviews.
ALTER FUNCTION integration.submit_test_data(jsonb,integer,text,text) RENAME TO submit_test_data_pre_archive;
REVOKE ALL ON FUNCTION integration.submit_test_data_pre_archive(jsonb,integer,text,text) FROM PUBLIC,__RP1_APP_USER__;
CREATE FUNCTION integration.submit_test_data(p jsonb,p_expected integer,p_key text,p_reason text)
RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,integration AS $$
BEGIN
    PERFORM pg_advisory_xact_lock(hashtextextended(p->>'execution_uuid',38));
    PERFORM 1 FROM integration.execution_archive WHERE execution_uuid=(p->>'execution_uuid')::uuid FOR UPDATE;
    RETURN integration.submit_test_data_pre_archive(p,p_expected,p_key,p_reason);
END $$;
REVOKE ALL ON FUNCTION integration.submit_test_data(jsonb,integer,text,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION integration.submit_test_data(jsonb,integer,text,text) TO __RP1_APP_USER__;

-- IAM credentials stay private; business reviewers can see the actors named
-- in readable submission/review records without an IAM-table inner join.
CREATE FUNCTION integration.submission_person(p_user_id bigint)
RETURNS TABLE(username text,display_name text) LANGUAGE sql STABLE SECURITY DEFINER
SET search_path=pg_catalog,integration,iam AS $$
    SELECT u.username,u.display_name FROM iam.app_user u
    WHERE u.id=p_user_id AND iam.current_principal_kind()='HUMAN' AND iam.can_write_business()
      AND (EXISTS(SELECT 1 FROM integration.data_submission s WHERE s.submitter_id=u.id)
           OR EXISTS(SELECT 1 FROM integration.data_submission_review r WHERE r.reviewer_id=u.id))
$$;
REVOKE ALL ON FUNCTION integration.submission_person(bigint) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION integration.submission_person(bigint) TO __RP1_APP_USER__;

CREATE FUNCTION integration.validate_archive_submission(p jsonb, p_submitter bigint)
RETURNS bigint LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, integration AS $$
DECLARE a integration.execution_archive%ROWTYPE; expected jsonb; k text;
BEGIN
    SELECT * INTO a FROM integration.execution_archive
        WHERE execution_uuid=(p->>'execution_uuid')::uuid FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION '必须先归档原始CSV并完成核实' USING ERRCODE='22023'; END IF;
    IF a.owner_id IS DISTINCT FROM p_submitter THEN
        RAISE EXCEPTION '提交身份必须与归档所有人一致' USING ERRCODE='42501';
    END IF;
    expected := integration.archive_summary(a.id);
    IF (SELECT count(*) FROM jsonb_object_keys(p)) <> (SELECT count(*) FROM jsonb_object_keys(expected)) THEN
        RAISE EXCEPTION '提交字段与冻结归档不一致' USING ERRCODE='22023';
    END IF;
    FOR k IN SELECT jsonb_object_keys(expected) LOOP
        IF k IN ('started_at','ended_at') THEN
            IF (p->>k)::timestamptz IS DISTINCT FROM (expected->>k)::timestamptz THEN
                RAISE EXCEPTION '采集时间不可改写' USING ERRCODE='22023';
            END IF;
        ELSIF p->k IS DISTINCT FROM expected->k THEN
            RAISE EXCEPTION '字段 % 与冻结归档不一致',k USING ERRCODE='22023';
        END IF;
    END LOOP;
    RETURN a.id;
END $$;
REVOKE ALL ON FUNCTION integration.validate_archive_submission(jsonb,bigint) FROM PUBLIC;

CREATE FUNCTION integration.bind_archive_submission()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, integration AS $$
DECLARE archive bigint; s integration.data_submission%ROWTYPE; a integration.execution_archive%ROWTYPE;
BEGIN
    SELECT * INTO STRICT s FROM integration.data_submission WHERE id=NEW.submission_id;
    archive := integration.validate_archive_submission(NEW.payload,s.submitter_id);
    SELECT * INTO STRICT a FROM integration.execution_archive WHERE id=archive;
    INSERT INTO integration.archive_submission(archive_id,submission_id,submission_revision,verification_revision,classification_revision)
    VALUES(a.id,s.id,NEW.revision,a.verification_revision,a.classification_revision);
    UPDATE integration.execution_archive SET submission_id=s.id,updated_at=clock_timestamp() WHERE id=a.id;
    RETURN NEW;
END $$;
REVOKE ALL ON FUNCTION integration.bind_archive_submission() FROM PUBLIC;
CREATE TRIGGER trg_bind_archive_submission AFTER INSERT ON integration.data_submission_revision
    FOR EACH ROW EXECUTE FUNCTION integration.bind_archive_submission();

CREATE FUNCTION integration.check_archive_review()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, integration AS $$
DECLARE s integration.data_submission%ROWTYPE; a integration.execution_archive%ROWTYPE; snap integration.archive_submission%ROWTYPE; p jsonb;
BEGIN
    IF NEW.decision <> 'APPROVED' THEN RETURN NEW; END IF;
    SELECT * INTO STRICT s FROM integration.data_submission WHERE id=NEW.submission_id;
    SELECT payload INTO STRICT p FROM integration.data_submission_revision WHERE submission_id=s.id AND revision=NEW.revision;
    SELECT * INTO snap FROM integration.archive_submission WHERE submission_id=s.id AND submission_revision=NEW.revision;
    IF NOT FOUND THEN RAISE EXCEPTION '该版本没有中央归档证据，请先补录' USING ERRCODE='22023'; END IF;
    PERFORM integration.validate_archive_submission(p,s.submitter_id);
    SELECT * INTO STRICT a FROM integration.execution_archive WHERE id=snap.archive_id;
    IF snap.verification_revision IS DISTINCT FROM a.verification_revision
       OR snap.classification_revision IS DISTINCT FROM a.classification_revision THEN
        RAISE EXCEPTION '核实或归类版本已变化，不能批准旧版本' USING ERRCODE='P0001';
    END IF;
    RETURN NEW;
END $$;
REVOKE ALL ON FUNCTION integration.check_archive_review() FROM PUBLIC;
CREATE TRIGGER trg_check_archive_review BEFORE INSERT ON integration.data_submission_review
    FOR EACH ROW EXECUTE FUNCTION integration.check_archive_review();

-- Actual tester and human verdict are separate from machine completion status.
CREATE FUNCTION integration.apply_archive_result()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, integration, test AS $$
DECLARE v jsonb; s bigint;
BEGIN
    SELECT approved_submission_id INTO s FROM test.test_execution WHERE id=NEW.execution_id;
    IF s IS NULL THEN RETURN NEW; END IF;
    SELECT av.payload INTO v FROM integration.data_submission ds
    JOIN integration.archive_submission snap ON snap.submission_id=ds.id AND snap.submission_revision=ds.revision
    JOIN integration.archive_verification av ON av.archive_id=snap.archive_id AND av.revision=snap.verification_revision
    WHERE ds.id=s;
    IF v IS NULL THEN
        -- Historical approved records without an archive remain readable, but
        -- a pending legacy summary cannot newly publish without evidence.
        IF EXISTS(SELECT 1 FROM integration.data_submission WHERE id=s AND status='PENDING') THEN
            RAISE EXCEPTION '正式发布缺少冻结核实版本' USING ERRCODE='22023';
        END IF;
        RETURN NEW;
    END IF;
    NEW.executor_display := v->>'tester';
    NEW.outcome := CASE v->>'verdict' WHEN 'passed' THEN 'PASSED' WHEN 'failed' THEN 'FAILED' ELSE 'INCONCLUSIVE' END;
    NEW.archive_status := 'ARCHIVED';
    RETURN NEW;
END $$;
REVOKE ALL ON FUNCTION integration.apply_archive_result() FROM PUBLIC;
CREATE TRIGGER trg_apply_archive_result BEFORE INSERT OR UPDATE ON test.execution_result
    FOR EACH ROW EXECUTE FUNCTION integration.apply_archive_result();

CREATE OR REPLACE FUNCTION reliability.interval_disposition(p_interval_id bigint,p_scope_id bigint)
RETURNS text LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, reliability AS $$
    SELECT CASE
        WHEN p_scope_id IS NULL THEN 'EXCLUDED'
        WHEN NOT EXISTS (SELECT 1 FROM test.runtime_interval ri WHERE ri.id=p_interval_id AND iam.can_access_campaign(ri.campaign_id,false)) THEN 'EXCLUDED'
        WHEN EXISTS (
            SELECT 1 FROM test.runtime_interval ri
            LEFT JOIN test.test_execution e ON e.id=ri.execution_id
            LEFT JOIN integration.data_submission s ON s.id=e.approved_submission_id
            WHERE ri.id=p_interval_id AND ri.source_method IN ('hmi-live-heartbeat','approved-summary-unqualified')
              AND s.status IS DISTINCT FROM 'APPROVED'
        ) THEN 'PENDING'
        WHEN EXISTS (
            SELECT 1 FROM reliability.exposure_assessment ea
            JOIN reliability.exposure_scope_assignment esa ON esa.assessment_id=ea.id
            WHERE ea.runtime_interval_id=p_interval_id AND esa.scope_id=p_scope_id
              AND ea.eligible AND ea.assignment_status='CONFIRMED'
              AND ea.quality_status IN ('VALID','PARTIAL') AND esa.inclusion_status='INCLUDED'
        ) THEN 'INCLUDED'
        WHEN EXISTS (
            SELECT 1 FROM reliability.exposure_assessment ea
            JOIN reliability.exposure_scope_assignment esa ON esa.assessment_id=ea.id AND esa.scope_id=p_scope_id
            WHERE ea.runtime_interval_id=p_interval_id
              AND (ea.assignment_status IN ('INVALID','REJECTED') OR ea.quality_status IN ('INVALID','MISSING') OR esa.inclusion_status='EXCLUDED')
              AND ea.assignment_status IS DISTINCT FROM 'PENDING_CONFIG_REVIEW'
              AND esa.inclusion_status IS DISTINCT FROM 'PENDING'
        ) THEN 'EXCLUDED'
        ELSE 'PENDING'
    END
$$;
REVOKE ALL ON FUNCTION reliability.interval_disposition(bigint,bigint) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION reliability.interval_disposition(bigint,bigint) TO __RP1_APP_USER__;
