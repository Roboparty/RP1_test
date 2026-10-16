-- Retrospective references append versions; they never republish measurements.
CREATE TABLE integration.archive_analysis_version (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    public_id uuid NOT NULL DEFAULT public.uuid_v7() UNIQUE,
    archive_id bigint NOT NULL REFERENCES integration.execution_archive(id),
    version integer NOT NULL CHECK(version>0),
    source_version_id bigint NOT NULL REFERENCES catalog.test_case_version(id),
    source_test_case_version_id uuid NOT NULL,
    source_case_code text NOT NULL,
    source_case_name text NOT NULL,
    source_version text NOT NULL,
    source_kind text NOT NULL DEFAULT 'PUBLISHED_CASE' CHECK(source_kind='PUBLISHED_CASE'),
    rules_snapshot jsonb NOT NULL CHECK(jsonb_typeof(rules_snapshot)='object' AND octet_length(rules_snapshot::text)<=262144),
    acceptance jsonb NOT NULL CHECK(jsonb_typeof(acceptance)='object' AND octet_length(acceptance::text)<=8192),
    mapping_status text NOT NULL CHECK(mapping_status IN ('MAPPED','UNCONFIGURED','INVALID')),
    mapping_notes text NOT NULL CHECK(length(mapping_notes)<=2000),
    report jsonb NOT NULL CHECK(jsonb_typeof(report)='object' AND octet_length(report::text)<=2097152),
    original_analysis_version text NOT NULL,
    original_sha256 text NOT NULL CHECK(original_sha256 ~ '^[0-9a-f]{64}$'),
    original_manifest_sha256 text NOT NULL CHECK(original_manifest_sha256 ~ '^[0-9a-f]{64}$'),
    actor_id bigint NOT NULL REFERENCES iam.app_user(id),
    reason text NOT NULL CHECK(length(btrim(reason)) BETWEEN 1 AND 300),
    idempotency_key text NOT NULL CHECK(length(idempotency_key) BETWEEN 8 AND 200),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE(archive_id,version),
    UNIQUE(actor_id,idempotency_key)
);
ALTER TABLE integration.archive_analysis_version ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON integration.archive_analysis_version FROM PUBLIC,__RP1_APP_USER__;
GRANT SELECT ON integration.archive_analysis_version TO __RP1_APP_USER__;
CREATE POLICY archive_analysis_read ON integration.archive_analysis_version FOR SELECT USING(integration.can_read_archive(archive_id));
CREATE TRIGGER archive_analysis_immutable BEFORE UPDATE OR DELETE ON integration.archive_analysis_version
    FOR EACH ROW EXECUTE FUNCTION integration.archive_immutable();
CREATE TRIGGER archive_analysis_audit AFTER INSERT OR UPDATE OR DELETE ON integration.archive_analysis_version
    FOR EACH ROW EXECUTE FUNCTION audit.capture_row_change();

CREATE FUNCTION integration.archive_published_acceptance(p_version bigint) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,catalog AS $$
DECLARE source catalog.test_case_version%ROWTYPE; candidate jsonb; rules jsonb; entry record; number numeric;
    status text:='MAPPED'; defaults jsonb:='{"criteria_version":"UNCONFIGURED","record_rate_hz":null,"required_active_seconds":null,"required_cycles":null,"min_sample_completeness":null,"max_gap_seconds":null,"max_temperature_rise_c":null,"max_tracking_rms_rad":null,"max_torque_abs_nm":null}'::jsonb;
BEGIN
    SELECT * INTO STRICT source FROM catalog.test_case_version WHERE id=p_version;
    candidate:=source.decision_rules->'archive_acceptance';
    IF candidate IS NULL OR candidate='null'::jsonb THEN status:='UNCONFIGURED';
    ELSIF jsonb_typeof(candidate)<>'object' THEN status:='INVALID';
    ELSE
        rules:=defaults||candidate||jsonb_build_object('criteria_version','CATALOG:'||source.public_id::text);
        IF (rules->'record_rate_hz')='null'::jsonb AND source.sampling_requirements ? 'record_rate_hz' THEN
            rules:=rules||jsonb_build_object('record_rate_hz',source.sampling_requirements->'record_rate_hz');
        END IF;
        FOR entry IN SELECT * FROM jsonb_each(rules) WHERE key<>'criteria_version' LOOP
            IF entry.key<>ALL(ARRAY['record_rate_hz','required_active_seconds','required_cycles','min_sample_completeness','max_gap_seconds','max_temperature_rise_c','max_tracking_rms_rad','max_torque_abs_nm']) THEN status:='INVALID'; EXIT; END IF;
            IF entry.value='null'::jsonb THEN CONTINUE; END IF;
            IF jsonb_typeof(entry.value)<>'number' THEN status:='INVALID'; EXIT; END IF;
            number:=(entry.value#>>'{}')::numeric;
            IF number<0 OR
                (entry.key='record_rate_hz' AND (number=0 OR number>100000)) OR
                (entry.key IN ('required_active_seconds','max_gap_seconds') AND number>315360000) OR
                (entry.key='required_cycles' AND (number>2147483647 OR trunc(number)<>number)) OR
                (entry.key='min_sample_completeness' AND number>1) OR
                (entry.key='max_temperature_rise_c' AND number>10000) OR
                (entry.key='max_tracking_rms_rad' AND number>1000000) OR
                (entry.key='max_torque_abs_nm' AND number>1000000000000) THEN status:='INVALID'; EXIT; END IF;
        END LOOP;
    END IF;
    RETURN jsonb_build_object('acceptance',CASE WHEN status='MAPPED' THEN rules ELSE defaults END,'mapping_status',status);
END $$;
REVOKE ALL ON FUNCTION integration.archive_published_acceptance(bigint) FROM PUBLIC,__RP1_APP_USER__;

CREATE FUNCTION integration.archive_add_analysis(p_id uuid,p_version uuid,p_acceptance jsonb,p_status text,p_notes text,p_report jsonb,p_reason text,p_key text)
RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,integration,iam,catalog,public AS $$
DECLARE a integration.execution_archive%ROWTYPE; receipt integration.archive_analysis_version%ROWTYPE;
    source catalog.test_case_version%ROWTYPE; tc catalog.test_case%ROWTYPE; next_version integer; result uuid; expected jsonb;
BEGIN
    IF iam.current_principal_kind()<>'HUMAN' OR NOT iam.can_write_business() THEN RAISE EXCEPTION '需要执行员或管理员身份' USING ERRCODE='42501'; END IF;
    IF p_id IS NULL OR p_version IS NULL OR p_key IS NULL OR length(p_key) NOT BETWEEN 8 AND 200 OR p_reason IS NULL OR length(btrim(p_reason)) NOT BETWEEN 1 AND 300
        OR p_status NOT IN ('MAPPED','UNCONFIGURED','INVALID') OR length(p_notes)>2000
        OR jsonb_typeof(p_acceptance)<>'object' OR octet_length(p_acceptance::text)>8192
        OR jsonb_typeof(p_report)<>'object' OR octet_length(p_report::text)>2097152 THEN RAISE EXCEPTION '重新分析参数无效' USING ERRCODE='22023'; END IF;
    PERFORM pg_advisory_xact_lock(hashtextextended(iam.current_user_id()::text||':'||p_key,40));
    SELECT * INTO a FROM integration.execution_archive WHERE public_id=p_id FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION '归档不存在' USING ERRCODE='P0002'; END IF;
    IF a.owner_id<>iam.current_user_id() AND NOT iam.is_system_admin() THEN RAISE EXCEPTION '仅原提交人或管理员可追加分析' USING ERRCODE='42501'; END IF;
    SELECT * INTO receipt FROM integration.archive_analysis_version WHERE actor_id=iam.current_user_id() AND idempotency_key=p_key;
    IF FOUND THEN
        IF receipt.archive_id<>a.id OR receipt.source_test_case_version_id<>p_version OR receipt.reason<>p_reason THEN RAISE EXCEPTION '重新分析幂等键内容冲突' USING ERRCODE='P0001'; END IF;
        RETURN receipt.public_id;
    END IF;
    IF a.processing_state<>'READY' OR a.upload_state<>'COMPLETE' OR a.manifest->>'original_sha256' IS DISTINCT FROM a.sha256 THEN RAISE EXCEPTION '基础归档校验尚未完成' USING ERRCODE='22023'; END IF;
    SELECT * INTO source FROM catalog.test_case_version WHERE public_id=p_version AND status='PUBLISHED' FOR SHARE;
    IF NOT FOUND THEN RAISE EXCEPTION '只能引用真实已发布用例版本' USING ERRCODE='22023'; END IF;
    SELECT * INTO tc FROM catalog.test_case WHERE id=source.test_case_id AND enabled FOR SHARE;
    IF NOT FOUND THEN RAISE EXCEPTION '用例已停用' USING ERRCODE='22023'; END IF;
    expected:=integration.archive_published_acceptance(source.id);
    IF p_acceptance IS DISTINCT FROM expected->'acceptance' OR p_status IS DISTINCT FROM expected->>'mapping_status' THEN RAISE EXCEPTION '数值判据必须来自该发布用例版本，不能注入手动规则' USING ERRCODE='22023'; END IF;
    IF p_report->>'scope' IS DISTINCT FROM 'RETROSPECTIVE_REFERENCE_ONLY'
        OR p_report->'timing' IS DISTINCT FROM a.facts
        OR p_report->>'mapping_status' IS DISTINCT FROM p_status
        OR (p_status='MAPPED' AND p_acceptance->>'criteria_version' IS DISTINCT FROM 'CATALOG:'||p_version::text)
        OR (p_status<>'MAPPED' AND p_acceptance->>'criteria_version' IS DISTINCT FROM 'UNCONFIGURED') THEN RAISE EXCEPTION '重新分析必须保留原始采集事实和判据来源' USING ERRCODE='22023'; END IF;
    SELECT coalesce(max(version),0)+1 INTO next_version FROM integration.archive_analysis_version WHERE archive_id=a.id;
    INSERT INTO integration.archive_analysis_version(archive_id,version,source_version_id,source_test_case_version_id,source_case_code,source_case_name,source_version,
        rules_snapshot,acceptance,mapping_status,mapping_notes,report,original_analysis_version,original_sha256,original_manifest_sha256,actor_id,reason,idempotency_key)
    VALUES(a.id,next_version,source.id,source.public_id,tc.case_code,tc.name,source.version,
        jsonb_build_object('decision_rules',source.decision_rules,'sampling_requirements',source.sampling_requirements),
        p_acceptance,p_status,p_notes,p_report,a.manifest#>>'{analysis,version}',a.sha256,
        encode(public.digest(convert_to(a.manifest::text,'UTF8'),'sha256'),'hex'),iam.current_user_id(),p_reason,p_key)
    RETURNING public_id INTO result;
    RETURN result;
END $$;
REVOKE ALL ON FUNCTION integration.archive_add_analysis(uuid,uuid,jsonb,text,text,jsonb,text,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION integration.archive_add_analysis(uuid,uuid,jsonb,text,text,jsonb,text,text) TO __RP1_APP_USER__;
