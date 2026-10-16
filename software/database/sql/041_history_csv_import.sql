-- Historical CSV intake is deliberately separate from the strict HMI contract.
-- No migration rewrites existing evidence or invents missing acquisition facts.
CREATE FUNCTION integration.archive_import(p jsonb, p_key text)
RETURNS TABLE(archive_id uuid, reused boolean)
LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,integration,iam,public AS $$
DECLARE
    a integration.execution_archive%ROWTYPE;
    receipt integration.archive_request%ROWTYPE;
    request_digest text;
    station_code text;
    generated_uuid uuid;
    ctx record;
BEGIN
    IF iam.current_principal_kind()<>'HUMAN' OR NOT iam.can_write_business() THEN
        RAISE EXCEPTION '需要执行员或管理员' USING ERRCODE='42501';
    END IF;
    IF p IS NULL OR jsonb_typeof(p) IS DISTINCT FROM 'object' OR octet_length(p::text)>16384
        OR p_key IS NULL OR length(p_key) NOT BETWEEN 8 AND 200 THEN
        RAISE EXCEPTION '补录参数无效' USING ERRCODE='22023';
    END IF;
    IF (SELECT count(*) FROM jsonb_object_keys(p))<>7
        OR NOT p ?& ARRAY['filename','size_bytes','sha256','test_name','actual_object','station_id','classification']
        OR jsonb_typeof(p->'filename') IS DISTINCT FROM 'string'
        OR jsonb_typeof(p->'sha256') IS DISTINCT FROM 'string'
        OR jsonb_typeof(p->'test_name') IS DISTINCT FROM 'string'
        OR jsonb_typeof(p->'actual_object') IS DISTINCT FROM 'string'
        OR jsonb_typeof(p->'station_id') IS DISTINCT FROM 'string'
        OR jsonb_typeof(p->'size_bytes') IS DISTINCT FROM 'number'
        OR (p->>'size_bytes')::numeric NOT BETWEEN 1 AND 53687091200
        OR trunc((p->>'size_bytes')::numeric)<>(p->>'size_bytes')::numeric
        OR p->>'sha256' !~ '^[0-9a-f]{64}$'
        OR length(p->>'filename') NOT BETWEEN 5 AND 200
        OR p->>'filename' ~ '[/\\]' OR lower(p->>'filename') NOT LIKE '%.csv'
        OR length(btrim(p->>'test_name')) NOT BETWEEN 1 AND 200
        OR length(btrim(p->>'actual_object')) NOT BETWEEN 1 AND 200 THEN
        RAISE EXCEPTION '补录仅接受文件身份、测试名称、对象、有效工位和可选正式关联' USING ERRCODE='22023';
    END IF;
    request_digest:=encode(public.digest(convert_to(jsonb_build_object('operation','import','payload',p)::text,'UTF8'),'sha256'),'hex');
    PERFORM pg_advisory_xact_lock(hashtextextended(iam.current_user_id()::text||':'||p_key,37));
    SELECT * INTO receipt FROM integration.archive_request
        WHERE actor_id=iam.current_user_id() AND idempotency_key=p_key;
    IF FOUND THEN
        IF receipt.request_hash<>request_digest OR receipt.operation<>'import' THEN
            RAISE EXCEPTION '补录幂等键内容冲突' USING ERRCODE='P0001';
        END IF;
        SELECT * INTO STRICT a FROM integration.execution_archive WHERE id=receipt.archive_id;
        IF NOT integration.can_read_archive(a.id) THEN
            RAISE EXCEPTION '归档不存在或不可访问' USING ERRCODE='42501';
        END IF;
        RETURN QUERY SELECT a.public_id,true;
        RETURN;
    END IF;
    -- Serializes distinct requests for identical bytes, including simultaneous
    -- browser tabs. Existing HMI records are reused, never rebound or edited.
    PERFORM pg_advisory_xact_lock(hashtextextended((p->>'sha256')||':'||(p->>'size_bytes'),41));
    SELECT * INTO a FROM integration.execution_archive
        WHERE sha256=p->>'sha256' AND size_bytes=(p->>'size_bytes')::bigint ORDER BY id LIMIT 1 FOR UPDATE;
    IF FOUND THEN
        IF NOT integration.can_read_archive(a.id) THEN
            RAISE EXCEPTION '归档不存在或不可访问' USING ERRCODE='42501';
        END IF;
        INSERT INTO integration.archive_request(actor_id,idempotency_key,request_hash,archive_id,operation)
            VALUES(iam.current_user_id(),p_key,request_digest,a.id,'import');
        RETURN QUERY SELECT a.public_id,true;
        RETURN;
    END IF;
    SELECT st.station_code INTO station_code
        FROM test.station st JOIN test.lab l ON l.id=st.lab_id JOIN test.site s ON s.id=l.site_id
        WHERE st.public_id=(p->>'station_id')::uuid AND st.enabled AND l.enabled AND s.enabled;
    IF NOT FOUND THEN RAISE EXCEPTION '请选择有效工位' USING ERRCODE='22023'; END IF;
    generated_uuid:=public.uuid_v7();
    INSERT INTO integration.execution_archive(execution_uuid,archive_code,owner_id,origin,test_name,actual_object,
        station_label,filename,size_bytes,sha256,facts,acceptance)
    VALUES(generated_uuid,'ARC-'||upper(replace(generated_uuid::text,'-','')),iam.current_user_id(),'HISTORY',
        btrim(p->>'test_name'),btrim(p->>'actual_object'),station_code,p->>'filename',(p->>'size_bytes')::bigint,p->>'sha256',
        jsonb_build_object('started_at',null,'ended_at',null,'active_seconds',null,'completed_cycles',null,
            'machine_status','unknown','automatic_verdict','unconfigured','data_quality','MISSING',
            'clock_quality','MISSING','exception_count',null,'summary',''),
        jsonb_build_object('criteria_version','UNCONFIGURED','record_rate_hz',null,'required_active_seconds',null,
            'required_cycles',null,'min_sample_completeness',null,'max_gap_seconds',null,
            'max_temperature_rise_c',null,'max_tracking_rms_rad',null,'max_torque_abs_nm',null)) RETURNING * INTO a;
    IF p->'classification'<>'null'::jsonb THEN
        IF jsonb_typeof(p->'classification') IS DISTINCT FROM 'object'
            OR (SELECT count(*) FROM jsonb_object_keys(p->'classification'))<>7
            OR p#>>'{classification,station_id}' IS DISTINCT FROM p->>'station_id' THEN
            RAISE EXCEPTION '补录正式关联工位必须与实际工位一致' USING ERRCODE='22023';
        END IF;
        SELECT * INTO ctx FROM integration.archive_context(p->'classification',a.id);
        IF NOT FOUND THEN RAISE EXCEPTION '正式执行上下文无效' USING ERRCODE='22023'; END IF;
        INSERT INTO integration.archive_classification(archive_id,revision,actor_id,payload,reason)
            VALUES(a.id,1,iam.current_user_id(),p->'classification','历史 CSV 补录关联');
        UPDATE integration.execution_archive SET revision=1,classification_revision=1 WHERE id=a.id;
    END IF;
    INSERT INTO integration.archive_request(actor_id,idempotency_key,request_hash,archive_id,operation)
        VALUES(iam.current_user_id(),p_key,request_digest,a.id,'import');
    RETURN QUERY SELECT a.public_id,false;
END $$;
REVOKE ALL ON FUNCTION integration.archive_import(jsonb,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION integration.archive_import(jsonb,text) TO __RP1_APP_USER__;

-- Null facts are admissible only to preserve incomplete history, never to
-- manufacture a formal execution or MTBF exposure through summary publication.
CREATE OR REPLACE FUNCTION integration.archive_summary(p_archive_id bigint)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, integration, iam AS $$
DECLARE a integration.execution_archive%ROWTYPE; v jsonb; c jsonb; starts timestamptz; ends timestamptz;
BEGIN
    IF iam.current_principal_kind() <> 'HUMAN' OR NOT iam.can_write_business() THEN
        RAISE EXCEPTION '需要执行员或管理员身份' USING ERRCODE='42501';
    END IF;
    SELECT * INTO a FROM integration.execution_archive WHERE id=p_archive_id FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION '归档不存在' USING ERRCODE='P0002'; END IF;
    IF a.origin='HISTORY' AND (a.facts->>'active_seconds' IS NULL OR a.facts->>'completed_cycles' IS NULL
        OR a.facts->>'exception_count' IS NULL OR a.facts->>'machine_status'='unknown') THEN
        RAISE EXCEPTION '历史 CSV 缺少运行秒数、循环数、异常数或机器终态；可核实归档查看曲线，不能送审或发布' USING ERRCODE='22023';
    END IF;
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
