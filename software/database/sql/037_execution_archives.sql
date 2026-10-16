-- Original CSV is immutable central evidence, independent of formal publication.
CREATE TABLE integration.execution_archive (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    public_id uuid NOT NULL DEFAULT public.uuid_v7() UNIQUE,
    execution_uuid uuid NOT NULL UNIQUE,
    archive_code text NOT NULL UNIQUE,
    owner_id bigint NOT NULL REFERENCES iam.app_user(id),
    origin text NOT NULL CHECK (origin IN ('HMI','HISTORY','OTHER')),
    test_name text NOT NULL CHECK (length(btrim(test_name)) BETWEEN 1 AND 200),
    actual_object text NOT NULL CHECK (length(btrim(actual_object)) BETWEEN 1 AND 200),
    station_label text NOT NULL CHECK (length(btrim(station_label)) BETWEEN 1 AND 100),
    filename text NOT NULL CHECK (length(filename) BETWEEN 5 AND 200 AND filename !~ '[/\\]' AND lower(filename) LIKE '%.csv'),
    size_bytes bigint NOT NULL CHECK (size_bytes BETWEEN 1 AND 53687091200),
    sha256 text NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    facts jsonb NOT NULL CHECK (jsonb_typeof(facts)='object' AND octet_length(facts::text)<=8192),
    acceptance jsonb NOT NULL CHECK (jsonb_typeof(acceptance)='object' AND octet_length(acceptance::text)<=8192),
    received_bytes bigint NOT NULL DEFAULT 0 CHECK (received_bytes>=0 AND received_bytes<=size_bytes),
    upload_state text NOT NULL DEFAULT 'UPLOADING' CHECK (upload_state IN ('UPLOADING','COMPLETE','CORRUPT')),
    processing_state text NOT NULL DEFAULT 'WAITING' CHECK (processing_state IN ('WAITING','QUEUED','PROCESSING','READY','FAILED')),
    processing_error text NOT NULL DEFAULT '',
    manifest jsonb NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(manifest)='object' AND octet_length(manifest::text)<=2097152),
    revision integer NOT NULL DEFAULT 0 CHECK (revision>=0),
    verification_revision integer,
    classification_revision integer,
    submission_id bigint REFERENCES integration.data_submission(id),
    lease_token uuid,
    lease_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CHECK (upload_state<>'COMPLETE' OR received_bytes=size_bytes),
    CHECK (processing_state NOT IN ('QUEUED','PROCESSING','READY') OR upload_state='COMPLETE')
);
CREATE INDEX ix_archive_queue ON integration.execution_archive(processing_state,id);
CREATE INDEX ix_archive_sha ON integration.execution_archive(sha256,size_bytes);

CREATE TABLE integration.archive_verification (
    archive_id bigint NOT NULL REFERENCES integration.execution_archive(id),
    revision integer NOT NULL CHECK (revision>0),
    actor_id bigint NOT NULL REFERENCES iam.app_user(id),
    payload jsonb NOT NULL CHECK (jsonb_typeof(payload)='object' AND octet_length(payload::text)<=16384),
    reason text NOT NULL CHECK (length(btrim(reason)) BETWEEN 1 AND 300),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY(archive_id,revision)
);
CREATE TABLE integration.archive_classification (
    archive_id bigint NOT NULL REFERENCES integration.execution_archive(id),
    revision integer NOT NULL CHECK (revision>0),
    actor_id bigint NOT NULL REFERENCES iam.app_user(id),
    payload jsonb NOT NULL CHECK (jsonb_typeof(payload)='object' AND octet_length(payload::text)<=4096),
    reason text NOT NULL CHECK (length(btrim(reason)) BETWEEN 1 AND 300),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY(archive_id,revision)
);
ALTER TABLE integration.execution_archive ADD CONSTRAINT fk_archive_verification
    FOREIGN KEY(id,verification_revision) REFERENCES integration.archive_verification(archive_id,revision) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE integration.execution_archive ADD CONSTRAINT fk_archive_classification
    FOREIGN KEY(id,classification_revision) REFERENCES integration.archive_classification(archive_id,revision) DEFERRABLE INITIALLY DEFERRED;
CREATE TABLE integration.archive_request (
    actor_id bigint NOT NULL REFERENCES iam.app_user(id),
    idempotency_key text NOT NULL CHECK (length(idempotency_key) BETWEEN 8 AND 200),
    request_hash text NOT NULL CHECK (request_hash ~ '^[0-9a-f]{64}$'),
    archive_id bigint NOT NULL REFERENCES integration.execution_archive(id),
    operation text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY(actor_id,idempotency_key)
);
CREATE TABLE integration.archive_submission (
    archive_id bigint NOT NULL REFERENCES integration.execution_archive(id),
    submission_id bigint NOT NULL,
    submission_revision integer NOT NULL,
    verification_revision integer NOT NULL,
    classification_revision integer NOT NULL,
    PRIMARY KEY(submission_id,submission_revision),
    FOREIGN KEY(submission_id,submission_revision) REFERENCES integration.data_submission_revision(submission_id,revision),
    FOREIGN KEY(archive_id,verification_revision) REFERENCES integration.archive_verification(archive_id,revision),
    FOREIGN KEY(archive_id,classification_revision) REFERENCES integration.archive_classification(archive_id,revision)
);

CREATE FUNCTION integration.can_read_archive(p_id bigint) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,integration,iam AS $$
    SELECT coalesce(iam.is_internal_service() OR (iam.current_principal_kind()='HUMAN' AND (
        iam.can_write_business() OR EXISTS (
            SELECT 1 FROM integration.execution_archive a JOIN integration.data_submission s ON s.id=a.submission_id
            WHERE a.id=p_id AND a.classification_revision IS NOT NULL AND s.status='APPROVED'
                AND iam.can_access_campaign(s.campaign_id,false)
        ))),false)
$$;

CREATE FUNCTION integration.archive_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_TABLE_NAME='execution_archive' THEN
        IF ROW(NEW.execution_uuid,NEW.archive_code,NEW.owner_id,NEW.origin,NEW.test_name,NEW.actual_object,
            NEW.station_label,NEW.filename,NEW.size_bytes,NEW.sha256,NEW.facts,NEW.acceptance)
            IS DISTINCT FROM ROW(OLD.execution_uuid,OLD.archive_code,OLD.owner_id,OLD.origin,OLD.test_name,OLD.actual_object,
            OLD.station_label,OLD.filename,OLD.size_bytes,OLD.sha256,OLD.facts,OLD.acceptance) THEN
            RAISE EXCEPTION '原始采集事实和归档身份不可修改' USING ERRCODE='22023';
        END IF;
        RETURN NEW;
    END IF;
    RAISE EXCEPTION '历史归档版本不可覆盖或删除' USING ERRCODE='22023';
END $$;
CREATE TRIGGER archive_immutable BEFORE UPDATE ON integration.execution_archive FOR EACH ROW EXECUTE FUNCTION integration.archive_immutable();
DO $$ DECLARE t text; BEGIN
    FOREACH t IN ARRAY ARRAY['execution_archive','archive_verification','archive_classification','archive_request','archive_submission'] LOOP
        EXECUTE format('ALTER TABLE integration.%I ENABLE ROW LEVEL SECURITY',t);
        EXECUTE format('REVOKE ALL ON integration.%I FROM PUBLIC,__RP1_APP_USER__',t);
        EXECUTE format('GRANT SELECT ON integration.%I TO __RP1_APP_USER__',t);
        EXECUTE format('CREATE TRIGGER trg_archive_audit AFTER INSERT OR UPDATE OR DELETE ON integration.%I FOR EACH ROW EXECUTE FUNCTION audit.capture_row_change()',t);
        IF t<>'execution_archive' THEN
            EXECUTE format('CREATE TRIGGER archive_immutable BEFORE UPDATE OR DELETE ON integration.%I FOR EACH ROW EXECUTE FUNCTION integration.archive_immutable()',t);
        END IF;
    END LOOP;
END $$;
CREATE POLICY archive_read ON integration.execution_archive FOR SELECT USING (integration.can_read_archive(id));
CREATE POLICY archive_read ON integration.archive_verification FOR SELECT USING (integration.can_read_archive(archive_id));
CREATE POLICY archive_read ON integration.archive_classification FOR SELECT USING (integration.can_read_archive(archive_id));
CREATE POLICY archive_read ON integration.archive_submission FOR SELECT USING (integration.can_read_archive(archive_id));
CREATE POLICY archive_request_read ON integration.archive_request FOR SELECT USING (actor_id=iam.current_user_id());

-- Pending submission tables intentionally exclude VIEWER; expose only the
-- approved metadata of a visible archive without weakening their RLS policies.
CREATE FUNCTION integration.archive_public_info(p_id bigint)
RETURNS TABLE(owner_public_id uuid,owner_name text,submission_public_id uuid,submission_revision integer,
    submission_status text,execution_public_id uuid,execution_code text,campaign_code text)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,integration,iam,test AS $$
    SELECT u.public_id,u.display_name,s.public_id,s.revision,s.status,e.public_id,e.execution_code,c.campaign_code
    FROM integration.execution_archive a JOIN iam.app_user u ON u.id=a.owner_id
    LEFT JOIN integration.data_submission s ON s.id=a.submission_id
    LEFT JOIN test.test_execution e ON e.approved_submission_id=s.id
    LEFT JOIN test.test_campaign c ON c.id=s.campaign_id
    WHERE a.id=p_id AND integration.can_read_archive(a.id)
$$;

CREATE FUNCTION integration.archive_actor_name(p_id bigint,p_actor bigint) RETURNS text
LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,integration,iam AS $$
    SELECT display_name FROM iam.app_user WHERE id=p_actor AND integration.can_read_archive(p_id)
$$;

CREATE FUNCTION integration.archive_create(p jsonb,p_key text) RETURNS uuid
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,integration,iam,public AS $$
DECLARE a integration.execution_archive%ROWTYPE; receipt integration.archive_request%ROWTYPE;
    digest text:=encode(public.digest(convert_to(jsonb_build_object('operation','create','payload',p)::text,'UTF8'),'sha256'),'hex');
    ctx record;
BEGIN
    IF iam.current_principal_kind()<>'HUMAN' OR NOT iam.can_write_business() THEN RAISE EXCEPTION '需要执行员或管理员' USING ERRCODE='42501'; END IF;
    IF p IS NULL OR jsonb_typeof(p)<>'object' OR octet_length(p::text)>32768 OR p_key IS NULL OR length(p_key) NOT BETWEEN 8 AND 200 THEN RAISE EXCEPTION '归档参数无效' USING ERRCODE='22023'; END IF;
    IF (SELECT count(*) FROM jsonb_object_keys(p))<>11 OR EXISTS(SELECT 1 FROM jsonb_object_keys(p) k WHERE k<>ALL(ARRAY['execution_uuid','test_name','actual_object','station_label','origin','filename','size_bytes','sha256','facts','acceptance','classification'])) THEN RAISE EXCEPTION '归档仅接受固定元数据字段' USING ERRCODE='22023'; END IF;
    IF jsonb_typeof(p->'facts')<>'object' OR (SELECT count(*) FROM jsonb_object_keys(p->'facts'))<>10 OR EXISTS(SELECT 1 FROM jsonb_object_keys(p->'facts') k WHERE k<>ALL(ARRAY['started_at','ended_at','active_seconds','completed_cycles','machine_status','automatic_verdict','data_quality','clock_quality','exception_count','summary']))
        OR EXISTS(SELECT 1 FROM jsonb_each(p->'facts') f WHERE jsonb_typeof(f.value) IN ('object','array'))
        OR EXISTS(SELECT 1 FROM jsonb_each(p->'facts') f WHERE f.value='null'::jsonb AND f.key NOT IN ('started_at','ended_at'))
        OR p#>>'{facts,machine_status}' NOT IN ('completed','failed','stopped','interrupted','unknown')
        OR p#>>'{facts,automatic_verdict}' NOT IN ('passed','failed','blocked','unconfigured')
        OR p#>>'{facts,data_quality}' NOT IN ('VALID','PARTIAL','INVALID','MISSING')
        OR p#>>'{facts,clock_quality}' NOT IN ('VALID','PARTIAL','INVALID','MISSING')
        OR jsonb_typeof(p#>'{facts,active_seconds}')<>'number' OR (p#>>'{facts,active_seconds}')::numeric NOT BETWEEN 0 AND 315360000
        OR jsonb_typeof(p#>'{facts,completed_cycles}')<>'number' OR (p#>>'{facts,completed_cycles}')::numeric NOT BETWEEN 0 AND 2147483647
        OR jsonb_typeof(p#>'{facts,exception_count}')<>'number' OR (p#>>'{facts,exception_count}')::numeric NOT BETWEEN 0 AND 2147483647
        OR jsonb_typeof(p#>'{facts,summary}')<>'string' OR length(p#>>'{facts,summary}')>500 THEN RAISE EXCEPTION '原始采集事实无效' USING ERRCODE='22023'; END IF;
    IF (p#>>'{facts,started_at}') IS NULL THEN
        IF p->>'origin'<>'HISTORY' OR (p#>>'{facts,ended_at}') IS NOT NULL OR p#>>'{facts,clock_quality}' NOT IN ('MISSING','INVALID') THEN RAISE EXCEPTION '缺失历史时间不得伪造可靠时钟' USING ERRCODE='22023'; END IF;
    ELSIF (p#>>'{facts,ended_at}') IS NULL OR NOT isfinite((p#>>'{facts,started_at}')::timestamptz) OR NOT isfinite((p#>>'{facts,ended_at}')::timestamptz)
        OR (p#>>'{facts,ended_at}')::timestamptz<(p#>>'{facts,started_at}')::timestamptz OR (p#>>'{facts,ended_at}')::timestamptz>clock_timestamp()+interval '5 minutes'
        OR (p#>>'{facts,active_seconds}')::numeric>extract(epoch FROM ((p#>>'{facts,ended_at}')::timestamptz-(p#>>'{facts,started_at}')::timestamptz))+.001 THEN RAISE EXCEPTION '采集时长或时间无效' USING ERRCODE='22023'; END IF;
    IF jsonb_typeof(p->'acceptance')<>'object' OR EXISTS(SELECT 1 FROM jsonb_object_keys(p->'acceptance') k WHERE k<>ALL(ARRAY['criteria_version','record_rate_hz','required_active_seconds','required_cycles','min_sample_completeness','max_gap_seconds','max_temperature_rise_c','max_tracking_rms_rad','max_torque_abs_nm']))
        OR EXISTS(SELECT 1 FROM jsonb_each(p->'acceptance') f WHERE jsonb_typeof(f.value) IN ('object','array'))
        OR jsonb_typeof(p#>'{acceptance,criteria_version}') IS DISTINCT FROM 'string'
        OR length(btrim(p#>>'{acceptance,criteria_version}')) NOT BETWEEN 1 AND 100 THEN RAISE EXCEPTION '只接受固定有界判据快照' USING ERRCODE='22023'; END IF;
    PERFORM pg_advisory_xact_lock(hashtextextended(iam.current_user_id()::text||':'||p_key,37));
    SELECT * INTO receipt FROM integration.archive_request WHERE actor_id=iam.current_user_id() AND idempotency_key=p_key;
    IF FOUND THEN
        IF receipt.request_hash<>digest THEN RAISE EXCEPTION '归档幂等键内容冲突' USING ERRCODE='P0001'; END IF;
        RETURN (SELECT public_id FROM integration.execution_archive WHERE id=receipt.archive_id);
    END IF;
    PERFORM pg_advisory_xact_lock(hashtextextended(p->>'execution_uuid',38));
    IF EXISTS(SELECT 1 FROM test.test_execution e WHERE e.acquisition_uuid=(p->>'execution_uuid')::uuid AND e.operator_id IS DISTINCT FROM iam.current_user_id()) THEN
        RAISE EXCEPTION '采集身份已绑定其他操作账号，不能用公开编号接管记录' USING ERRCODE='42501';
    END IF;
    SELECT * INTO a FROM integration.execution_archive WHERE execution_uuid=(p->>'execution_uuid')::uuid FOR UPDATE;
    IF FOUND THEN
        IF a.owner_id<>iam.current_user_id() AND NOT iam.is_system_admin() THEN RAISE EXCEPTION '执行身份已被其他账号归档' USING ERRCODE='42501'; END IF;
        IF a.sha256<>p->>'sha256' OR a.size_bytes<>(p->>'size_bytes')::bigint OR a.facts IS DISTINCT FROM p->'facts'
           OR a.acceptance IS DISTINCT FROM p->'acceptance' OR a.test_name<>p->>'test_name' OR a.actual_object<>p->>'actual_object'
           OR a.station_label<>p->>'station_label' OR a.filename<>p->>'filename' OR a.origin<>p->>'origin' THEN
            RAISE EXCEPTION '同一执行身份不能替换文件或采集事实' USING ERRCODE='P0001';
        END IF;
    ELSE
        INSERT INTO integration.execution_archive(execution_uuid,archive_code,owner_id,origin,test_name,actual_object,station_label,filename,size_bytes,sha256,facts,acceptance)
        VALUES((p->>'execution_uuid')::uuid,'ARC-'||upper(replace(p->>'execution_uuid','-','')),iam.current_user_id(),p->>'origin',p->>'test_name',p->>'actual_object',p->>'station_label',p->>'filename',(p->>'size_bytes')::bigint,p->>'sha256',p->'facts',p->'acceptance') RETURNING * INTO a;
        IF p->'classification' IS NOT NULL AND p->'classification'<>'null'::jsonb THEN
            IF (SELECT count(*) FROM jsonb_object_keys(p->'classification'))<>7 THEN RAISE EXCEPTION '归类字段无效' USING ERRCODE='22023'; END IF;
            SELECT * INTO ctx FROM integration.archive_context(p->'classification',a.id);
            IF NOT FOUND THEN RAISE EXCEPTION '执行上下文无效' USING ERRCODE='22023'; END IF;
            INSERT INTO integration.archive_classification(archive_id,revision,actor_id,payload,reason) VALUES(a.id,1,iam.current_user_id(),p->'classification','首次归档关联');
            UPDATE integration.execution_archive SET revision=1,classification_revision=1 WHERE id=a.id;
        END IF;
    END IF;
    INSERT INTO integration.archive_request(actor_id,idempotency_key,request_hash,archive_id,operation) VALUES(iam.current_user_id(),p_key,digest,a.id,'create');
    RETURN a.public_id;
END $$;

CREATE FUNCTION integration.archive_edit(p_id uuid,p_operation text,p jsonb,p_key text) RETURNS uuid
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,integration,iam,public AS $$
DECLARE a integration.execution_archive%ROWTYPE; receipt integration.archive_request%ROWTYPE; ctx record;
    old_payload jsonb; current_status text; new_revision integer;
    digest text:=encode(public.digest(convert_to(jsonb_build_object('id',p_id,'operation',p_operation,'payload',p)::text,'UTF8'),'sha256'),'hex');
BEGIN
    IF iam.current_principal_kind()<>'HUMAN' OR NOT iam.can_write_business() THEN RAISE EXCEPTION '需要执行员或管理员' USING ERRCODE='42501'; END IF;
    IF p IS NULL OR jsonb_typeof(p)<>'object' OR octet_length(p::text)>16384 OR p_key IS NULL OR length(p_key) NOT BETWEEN 8 AND 200 OR p_operation NOT IN ('verification','classification') OR length(btrim(p->>'reason')) NOT BETWEEN 1 AND 300 THEN RAISE EXCEPTION '核实参数无效' USING ERRCODE='22023'; END IF;
    PERFORM pg_advisory_xact_lock(hashtextextended(iam.current_user_id()::text||':'||p_key,37));
    SELECT * INTO receipt FROM integration.archive_request WHERE actor_id=iam.current_user_id() AND idempotency_key=p_key;
    IF FOUND THEN
        IF receipt.request_hash<>digest THEN RAISE EXCEPTION '归档幂等键内容冲突' USING ERRCODE='P0001'; END IF;
        RETURN (SELECT public_id FROM integration.execution_archive WHERE id=receipt.archive_id);
    END IF;
    SELECT * INTO a FROM integration.execution_archive WHERE public_id=p_id FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION '归档不存在' USING ERRCODE='P0002'; END IF;
    IF a.owner_id<>iam.current_user_id() AND NOT iam.is_system_admin() THEN RAISE EXCEPTION '仅提交人或管理员可核实归类' USING ERRCODE='42501'; END IF;
    SELECT status INTO current_status FROM integration.data_submission WHERE id=a.submission_id;
    IF coalesce(current_status,'') IN ('PENDING','APPROVED') OR a.revision<>(p->>'expected_revision')::integer THEN RAISE EXCEPTION '版本已变化或记录已送审' USING ERRCODE='P0001'; END IF;
    new_revision:=a.revision+1;
    IF p_operation='verification' THEN
        IF (SELECT count(*) FROM jsonb_object_keys(p))<>9 OR EXISTS(SELECT 1 FROM jsonb_object_keys(p) k WHERE k<>ALL(ARRAY['expected_revision','tester','station_id','verdict','notes','discrepancy_reason','declared_started_at','declared_ended_at','reason'])) THEN RAISE EXCEPTION '核实字段不得包含采集数据或编辑分析' USING ERRCODE='22023'; END IF;
        IF EXISTS(SELECT 1 FROM jsonb_each(p) f WHERE f.value='null'::jsonb AND f.key NOT IN ('declared_started_at','declared_ended_at'))
            OR jsonb_typeof(p->'tester')<>'string' OR jsonb_typeof(p->'notes')<>'string' OR jsonb_typeof(p->'discrepancy_reason')<>'string'
            OR length(p->>'discrepancy_reason')>1000 THEN RAISE EXCEPTION '核实字段类型无效' USING ERRCODE='22023'; END IF;
        IF a.processing_state<>'READY' THEN RAISE EXCEPTION '请先等待原始文件校验和分析完成' USING ERRCODE='P0001'; END IF;
        IF p->>'verdict' NOT IN ('passed','failed','blocked') OR length(btrim(p->>'tester')) NOT BETWEEN 1 AND 100 OR length(btrim(p->>'notes')) NOT BETWEEN 1 AND 2000
            OR NOT EXISTS(SELECT 1 FROM test.station st JOIN test.lab l ON l.id=st.lab_id JOIN test.site s ON s.id=l.site_id WHERE st.public_id=(p->>'station_id')::uuid AND st.enabled AND l.enabled AND s.enabled) THEN RAISE EXCEPTION '核实人、记录或有效工位无效' USING ERRCODE='22023'; END IF;
        IF p->>'verdict' IS DISTINCT FROM a.manifest#>>'{analysis,verdict}' AND length(btrim(coalesce(p->>'discrepancy_reason','')))=0 THEN RAISE EXCEPTION '人工结论与自动分析不同，必须填写原因' USING ERRCODE='22023'; END IF;
        IF (a.facts->>'started_at') IS NOT NULL AND ((p->>'declared_started_at') IS NOT NULL OR (p->>'declared_ended_at') IS NOT NULL) THEN RAISE EXCEPTION '已存在真实采集时间，不能补录覆盖' USING ERRCODE='22023'; END IF;
        IF (p->>'declared_started_at') IS NOT NULL AND ((p->>'declared_ended_at')::timestamptz<(p->>'declared_started_at')::timestamptz OR (a.facts->>'active_seconds')::numeric>extract(epoch FROM ((p->>'declared_ended_at')::timestamptz-(p->>'declared_started_at')::timestamptz))+.001) THEN RAISE EXCEPTION '人工补录时间与有效时长冲突' USING ERRCODE='22023'; END IF;
        INSERT INTO integration.archive_verification(archive_id,revision,actor_id,payload,reason) VALUES(a.id,new_revision,iam.current_user_id(),p-'expected_revision'-'reason',p->>'reason');
        UPDATE integration.execution_archive SET revision=new_revision,verification_revision=new_revision,updated_at=clock_timestamp() WHERE id=a.id;
    ELSE
        IF (SELECT count(*) FROM jsonb_object_keys(p))<>3 OR (SELECT count(*) FROM jsonb_object_keys(p->'context'))<>7 THEN RAISE EXCEPTION '归类字段无效' USING ERRCODE='22023'; END IF;
        SELECT * INTO ctx FROM integration.archive_context(p->'context',a.id);
        IF NOT FOUND THEN RAISE EXCEPTION '执行上下文无效或已停用' USING ERRCODE='22023'; END IF;
        SELECT payload INTO old_payload FROM integration.archive_verification WHERE archive_id=a.id AND revision=a.verification_revision;
        IF old_payload IS NOT NULL AND old_payload->>'station_id'<>p#>>'{context,station_id}' THEN RAISE EXCEPTION '归类工位必须与核实工位一致' USING ERRCODE='22023'; END IF;
        INSERT INTO integration.archive_classification(archive_id,revision,actor_id,payload,reason) VALUES(a.id,new_revision,iam.current_user_id(),p->'context',p->>'reason');
        UPDATE integration.execution_archive SET revision=new_revision,classification_revision=new_revision,updated_at=clock_timestamp() WHERE id=a.id;
    END IF;
    INSERT INTO integration.archive_request(actor_id,idempotency_key,request_hash,archive_id,operation) VALUES(iam.current_user_id(),p_key,digest,a.id,p_operation);
    RETURN a.public_id;
END $$;

-- Upload updates are owner-authorized and serialized with a PostgreSQL row lock.
CREATE FUNCTION integration.archive_lock(p_id uuid) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,integration,iam AS $$
DECLARE a integration.execution_archive%ROWTYPE;
BEGIN
    SELECT * INTO a FROM integration.execution_archive WHERE public_id=p_id FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION '归档不存在' USING ERRCODE='P0002'; END IF;
    IF iam.current_principal_kind()<>'HUMAN' OR NOT iam.can_write_business() OR (a.owner_id<>iam.current_user_id() AND NOT iam.is_system_admin()) THEN RAISE EXCEPTION '无归档写权限' USING ERRCODE='42501'; END IF;
END $$;
CREATE FUNCTION integration.archive_upload(p_id uuid,p_received bigint,p_state text) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,integration,iam AS $$
DECLARE a integration.execution_archive%ROWTYPE;
BEGIN
    SELECT * INTO a FROM integration.execution_archive WHERE public_id=p_id FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION '归档不存在' USING ERRCODE='P0002'; END IF;
    IF iam.current_principal_kind()<>'HUMAN' OR NOT iam.can_write_business() OR (a.owner_id<>iam.current_user_id() AND NOT iam.is_system_admin()) THEN RAISE EXCEPTION '无归档写权限' USING ERRCODE='42501'; END IF;
    IF p_received NOT BETWEEN 0 AND a.size_bytes OR p_state NOT IN ('UPLOADING','COMPLETE','CORRUPT')
        OR p_received<a.received_bytes
        OR (a.upload_state='COMPLETE' AND (p_state<>'COMPLETE' OR p_received<>a.size_bytes))
        OR (p_state='COMPLETE' AND p_received<>a.size_bytes) THEN RAISE EXCEPTION '不能替换已归档文件' USING ERRCODE='P0001'; END IF;
    UPDATE integration.execution_archive SET received_bytes=p_received,upload_state=p_state,
        processing_state=CASE WHEN p_state='COMPLETE' AND processing_state IN ('WAITING','FAILED') THEN 'QUEUED' ELSE processing_state END,
        processing_error=CASE WHEN p_state='COMPLETE' AND processing_state='FAILED' THEN '' ELSE processing_error END,
        updated_at=clock_timestamp() WHERE id=a.id;
END $$;

CREATE FUNCTION integration.archive_claim(p_token uuid) RETURNS SETOF integration.execution_archive
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,integration,iam AS $$
BEGIN
    IF NOT iam.is_internal_service() THEN RAISE EXCEPTION '仅内部归档处理服务可领取' USING ERRCODE='42501'; END IF;
    RETURN QUERY UPDATE integration.execution_archive a SET processing_state='PROCESSING',lease_token=p_token,lease_at=clock_timestamp(),updated_at=clock_timestamp()
        WHERE a.id=(SELECT q.id FROM integration.execution_archive q WHERE q.processing_state='QUEUED' OR (q.processing_state='PROCESSING' AND q.lease_at<clock_timestamp()-interval '2 minutes') ORDER BY q.id FOR UPDATE SKIP LOCKED LIMIT 1) RETURNING a.*;
END $$;
CREATE FUNCTION integration.archive_progress(p_id uuid,p_token uuid,p_manifest jsonb,p_error text) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,integration,iam AS $$
BEGIN
    IF NOT iam.is_internal_service() THEN RAISE EXCEPTION '仅内部归档处理服务可更新' USING ERRCODE='42501'; END IF;
    UPDATE integration.execution_archive SET lease_at=clock_timestamp(),
        processing_state=CASE WHEN p_error IS NOT NULL THEN 'FAILED' WHEN p_manifest IS NOT NULL THEN 'READY' ELSE 'PROCESSING' END,
        upload_state=CASE WHEN p_error='CHECKSUM_MISMATCH' THEN 'CORRUPT' ELSE upload_state END,
        received_bytes=CASE WHEN p_error='CHECKSUM_MISMATCH' THEN 0 ELSE received_bytes END,
        processing_error=left(coalesce(p_error,''),1000),manifest=coalesce(p_manifest,manifest),updated_at=clock_timestamp()
    WHERE public_id=p_id AND lease_token=p_token AND processing_state='PROCESSING';
    RETURN FOUND;
END $$;

REVOKE ALL ON FUNCTION integration.can_read_archive(bigint),integration.archive_public_info(bigint),integration.archive_actor_name(bigint,bigint),integration.archive_immutable(),integration.archive_create(jsonb,text),integration.archive_edit(uuid,text,jsonb,text),integration.archive_lock(uuid),integration.archive_upload(uuid,bigint,text),integration.archive_claim(uuid),integration.archive_progress(uuid,uuid,jsonb,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION integration.can_read_archive(bigint),integration.archive_public_info(bigint),integration.archive_actor_name(bigint,bigint),integration.archive_create(jsonb,text),integration.archive_edit(uuid,text,jsonb,text),integration.archive_lock(uuid),integration.archive_upload(uuid,bigint,text),integration.archive_claim(uuid),integration.archive_progress(uuid,uuid,jsonb,text) TO __RP1_APP_USER__;
