-- Edge collector service credentials and idempotent ingestion support.
-- Secret material is HMAC-SHA256 hashed by the API before it crosses the
-- database boundary. No plaintext API key is stored or audited.

CREATE TABLE iam.service_credential (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    public_id uuid NOT NULL DEFAULT public.uuid_v7() UNIQUE,
    service_user_id bigint NOT NULL REFERENCES iam.app_user(id) ON DELETE CASCADE,
    key_id text NOT NULL UNIQUE,
    secret_hash text NOT NULL,
    hash_algorithm text NOT NULL DEFAULT 'HMAC-SHA256'
        CHECK (hash_algorithm = 'HMAC-SHA256'),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    expires_at timestamptz,
    last_used_at timestamptz,
    rotated_from_id bigint REFERENCES iam.service_credential(id),
    revoked_at timestamptz,
    revoked_by bigint REFERENCES iam.app_user(id),
    revoke_reason text NOT NULL DEFAULT '',
    CHECK (key_id ~ '^[A-Za-z0-9_-]{12,64}$'),
    CHECK (secret_hash ~ '^[0-9a-f]{64}$'),
    CHECK (expires_at IS NULL OR expires_at > created_at),
    CHECK (
        revoked_at IS NULL
        OR (btrim(revoke_reason) <> '' AND revoked_at >= created_at)
    )
);

CREATE INDEX ix_service_credential_active
    ON iam.service_credential(key_id, service_user_id)
    WHERE revoked_at IS NULL;
CREATE INDEX ix_service_credential_user_created
    ON iam.service_credential(service_user_id, created_at DESC);

CREATE TABLE integration.service_principal_source (
    service_user_id bigint PRIMARY KEY
        REFERENCES iam.app_user(id) ON DELETE CASCADE,
    source_id bigint NOT NULL UNIQUE
        REFERENCES integration.ingestion_source(id),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

ALTER TABLE test.test_execution
    ADD COLUMN ingestion_segment_id bigint REFERENCES test.analysis_segment(id);
CREATE INDEX ix_execution_ingestion_segment
    ON test.test_execution(ingestion_segment_id)
    WHERE ingestion_segment_id IS NOT NULL;

-- EDGE_COLLECTOR_SUBJECT_SEEDS_BEGIN
-- Stable producer-facing joint directory. Existing 018 rows retain their
-- subject_code and display_order; additions fill the remaining module DoF.
INSERT INTO catalog.telemetry_subject(
    subject_code, name, subject_kind, target_part_code, display_order
)
VALUES
    ('SARM-SHOULDER-PITCH', '单臂肩部俯仰关节', 'JOINT', 'SARM', 1),
    ('SARM-ELBOW-PITCH', '单臂肘部俯仰关节', 'JOINT', 'SARM', 2),
    ('SARM-WRIST-ROLL', '单臂腕部滚转关节', 'JOINT', 'SARM', 3),
    ('SARM-SHOULDER-ROLL', '单臂肩部滚转关节', 'JOINT', 'SARM', 4),
    ('SARM-SHOULDER-YAW', '单臂肩部偏航关节', 'JOINT', 'SARM', 5),
    ('SARM-WRIST-YAW', '单臂腕部偏航关节', 'JOINT', 'SARM', 6),
    ('SARM-WRIST-PITCH', '单臂腕部俯仰关节', 'JOINT', 'SARM', 7),
    ('SLEG-HIP-PITCH', '单腿髋部俯仰关节', 'JOINT', 'SLEG', 1),
    ('SLEG-KNEE-PITCH', '单腿膝部俯仰关节', 'JOINT', 'SLEG', 2),
    ('SLEG-ANKLE-PITCH', '单腿踝部俯仰关节', 'JOINT', 'SLEG', 3),
    ('SLEG-HIP-ROLL', '单腿髋部滚转关节', 'JOINT', 'SLEG', 4),
    ('SLEG-HIP-YAW', '单腿髋部偏航关节', 'JOINT', 'SLEG', 5),
    ('SLEG-ANKLE-ROLL', '单腿踝部滚转关节', 'JOINT', 'SLEG', 6),
    ('UPPER-SHOULDER-L', '上肢左肩联动关节', 'JOINT', 'UPPER', 1),
    ('UPPER-SHOULDER-R', '上肢右肩联动关节', 'JOINT', 'UPPER', 2),
    ('UPPER-WAIST-YAW', '上肢腰部偏航关节', 'JOINT', 'UPPER', 3),
    ('UPPER-SHOULDER-PITCH-L', '上肢左肩俯仰关节', 'JOINT', 'UPPER', 4),
    ('UPPER-SHOULDER-PITCH-R', '上肢右肩俯仰关节', 'JOINT', 'UPPER', 5),
    ('UPPER-ELBOW-PITCH-L', '上肢左肘俯仰关节', 'JOINT', 'UPPER', 6),
    ('UPPER-ELBOW-PITCH-R', '上肢右肘俯仰关节', 'JOINT', 'UPPER', 7),
    ('UPPER-WRIST-ROLL-L', '上肢左腕滚转关节', 'JOINT', 'UPPER', 8),
    ('UPPER-WRIST-ROLL-R', '上肢右腕滚转关节', 'JOINT', 'UPPER', 9),
    ('UPPER-WRIST-YAW-L', '上肢左腕偏航关节', 'JOINT', 'UPPER', 10),
    ('UPPER-WRIST-YAW-R', '上肢右腕偏航关节', 'JOINT', 'UPPER', 11),
    ('UPPER-WAIST-PITCH', '上肢腰部俯仰关节', 'JOINT', 'UPPER', 12),
    ('LOWER-HIP-L', '下肢左髋联动关节', 'JOINT', 'LOWER', 1),
    ('LOWER-HIP-R', '下肢右髋联动关节', 'JOINT', 'LOWER', 2),
    ('LOWER-WAIST-PITCH', '下肢腰部俯仰关节', 'JOINT', 'LOWER', 3),
    ('LOWER-HIP-PITCH-L', '下肢左髋俯仰关节', 'JOINT', 'LOWER', 4),
    ('LOWER-HIP-PITCH-R', '下肢右髋俯仰关节', 'JOINT', 'LOWER', 5),
    ('LOWER-HIP-ROLL-L', '下肢左髋滚转关节', 'JOINT', 'LOWER', 6),
    ('LOWER-HIP-ROLL-R', '下肢右髋滚转关节', 'JOINT', 'LOWER', 7),
    ('LOWER-HIP-YAW-L', '下肢左髋偏航关节', 'JOINT', 'LOWER', 8),
    ('LOWER-HIP-YAW-R', '下肢右髋偏航关节', 'JOINT', 'LOWER', 9),
    ('LOWER-KNEE-PITCH-L', '下肢左膝俯仰关节', 'JOINT', 'LOWER', 10),
    ('LOWER-KNEE-PITCH-R', '下肢右膝俯仰关节', 'JOINT', 'LOWER', 11),
    ('LOWER-ANKLE-PITCH-L', '下肢左踝俯仰关节', 'JOINT', 'LOWER', 12),
    ('LOWER-ANKLE-PITCH-R', '下肢右踝俯仰关节', 'JOINT', 'LOWER', 13),
    ('LOWER-WAIST-YAW', '下肢腰部偏航关节', 'JOINT', 'LOWER', 14)
ON CONFLICT (subject_code) DO UPDATE SET
    name = EXCLUDED.name,
    subject_kind = EXCLUDED.subject_kind,
    target_part_code = EXCLUDED.target_part_code,
    display_order = EXCLUDED.display_order,
    enabled = true,
    updated_at = clock_timestamp();
-- EDGE_COLLECTOR_SUBJECT_SEEDS_END

CREATE TABLE integration.ingestion_batch (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    public_id uuid NOT NULL DEFAULT public.uuid_v7() UNIQUE,
    source_id bigint NOT NULL REFERENCES integration.ingestion_source(id),
    producer_batch_key text NOT NULL,
    execution_id bigint REFERENCES test.test_execution(id),
    batch_kind text NOT NULL CHECK (
        batch_kind IN ('EVENTS', 'TELEMETRY', 'MANIFEST', 'EVIDENCE', 'FINISH')
    ),
    payload_hash text NOT NULL CHECK (payload_hash ~ '^[0-9a-f]{64}$'),
    item_count integer NOT NULL DEFAULT 0 CHECK (item_count >= 0),
    accepted_count integer NOT NULL DEFAULT 0 CHECK (
        accepted_count >= 0 AND accepted_count <= item_count
    ),
    status text NOT NULL CHECK (
        status IN ('RECEIVING', 'ACCEPTED', 'PARTIAL', 'REJECTED')
    ),
    source_time timestamptz,
    received_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    completed_at timestamptz,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb
        CHECK (jsonb_typeof(metadata) = 'object'),
    error_detail text NOT NULL DEFAULT '',
    UNIQUE (source_id, producer_batch_key),
    CHECK (btrim(producer_batch_key) <> ''),
    CHECK (completed_at IS NULL OR completed_at >= received_at)
);

ALTER TABLE integration.ingestion_receipt
    ADD COLUMN batch_id bigint REFERENCES integration.ingestion_batch(id),
    ADD COLUMN request_kind text NOT NULL DEFAULT 'LEGACY',
    ADD COLUMN producer_key text,
    ADD COLUMN http_status integer,
    ADD COLUMN response_body jsonb;

ALTER TABLE integration.ingestion_receipt
    ADD CONSTRAINT ck_ingestion_receipt_request_kind
        CHECK (request_kind ~ '^[A-Z][A-Z0-9_]{1,63}$'),
    ADD CONSTRAINT ck_ingestion_receipt_producer_key
        CHECK (producer_key IS NULL OR btrim(producer_key) <> ''),
    ADD CONSTRAINT ck_ingestion_receipt_http_status
        CHECK (http_status IS NULL OR http_status BETWEEN 100 AND 599),
    ADD CONSTRAINT ck_ingestion_receipt_response_body
        CHECK (response_body IS NULL OR jsonb_typeof(response_body) = 'object');

CREATE UNIQUE INDEX uq_ingestion_receipt_producer_key
    ON integration.ingestion_receipt(source_id, request_kind, producer_key)
    WHERE producer_key IS NOT NULL;
CREATE INDEX ix_ingestion_receipt_batch
    ON integration.ingestion_receipt(batch_id, received_at DESC)
    WHERE batch_id IS NOT NULL;

ALTER TABLE integration.artifact
    ADD COLUMN source_id bigint REFERENCES integration.ingestion_source(id),
    ADD COLUMN source_artifact_key text,
    ADD COLUMN metadata jsonb NOT NULL DEFAULT '{}'::jsonb;

ALTER TABLE integration.artifact
    ADD CONSTRAINT ck_artifact_sha256
        CHECK (sha256 IS NULL OR sha256 ~ '^[0-9a-f]{64}$'),
    ADD CONSTRAINT ck_artifact_source_key_pair
        CHECK ((source_id IS NULL) = (source_artifact_key IS NULL)),
    ADD CONSTRAINT ck_artifact_metadata_object
        CHECK (jsonb_typeof(metadata) = 'object');

CREATE UNIQUE INDEX uq_artifact_source_key
    ON integration.artifact(source_id, source_artifact_key)
    WHERE source_id IS NOT NULL AND source_artifact_key IS NOT NULL;
CREATE INDEX ix_artifact_sha256
    ON integration.artifact(sha256)
    WHERE sha256 IS NOT NULL;

CREATE TABLE integration.trajectory_version (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    public_id uuid NOT NULL DEFAULT public.uuid_v7() UNIQUE,
    source_id bigint NOT NULL REFERENCES integration.ingestion_source(id),
    campaign_id bigint NOT NULL REFERENCES test.test_campaign(id),
    asset_id bigint NOT NULL REFERENCES test.asset(id),
    execution_id bigint NOT NULL REFERENCES test.test_execution(id),
    artifact_id bigint REFERENCES integration.artifact(id),
    trajectory_key text NOT NULL,
    version integer NOT NULL CHECK (version > 0),
    file_sha256 text NOT NULL CHECK (file_sha256 ~ '^[0-9a-f]{64}$'),
    file_size_bytes bigint CHECK (file_size_bytes IS NULL OR file_size_bytes >= 0),
    coordinate_frame text NOT NULL DEFAULT '',
    point_count bigint CHECK (point_count IS NULL OR point_count >= 0),
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb
        CHECK (jsonb_typeof(metadata) = 'object'),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (source_id, trajectory_key, version)
);

CREATE INDEX ix_trajectory_execution_version
    ON integration.trajectory_version(execution_id, trajectory_key, version DESC);
CREATE INDEX ix_trajectory_hash
    ON integration.trajectory_version(file_sha256);

CREATE OR REPLACE FUNCTION iam.current_ingestion_source_id()
RETURNS bigint
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, iam, integration
AS $$
    SELECT mapping.source_id
    FROM integration.service_principal_source mapping
    WHERE mapping.service_user_id = iam.current_user_id()
      AND iam.current_principal_kind() = 'SERVICE'
$$;

CREATE OR REPLACE FUNCTION iam.can_ingest_source(p_source_id bigint)
RETURNS boolean
LANGUAGE sql
STABLE
AS $$
    SELECT coalesce(
        iam.is_system_admin()
        OR (
            iam.current_principal_kind() = 'SERVICE'
            AND iam.current_ingestion_source_id() = p_source_id
        ),
        false
    )
$$;

CREATE OR REPLACE FUNCTION iam.auth_resolve_service_credential(
    p_key_id text,
    p_secret_hash text
)
RETURNS TABLE (
    user_public_id uuid,
    username text,
    display_name text,
    role text,
    principal_kind text,
    credential_public_id uuid,
    source_public_id uuid,
    source_code text
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, iam, integration
AS $$
BEGIN
    IF p_key_id !~ '^[A-Za-z0-9_-]{12,64}$'
       OR p_secret_hash !~ '^[0-9a-f]{64}$' THEN
        RETURN;
    END IF;

    RETURN QUERY
    WITH resolved AS (
        SELECT
            credential.id AS credential_id,
            user_account.public_id AS user_public_id,
            user_account.username,
            user_account.display_name,
            user_account.role,
            user_account.principal_kind,
            credential.public_id AS credential_public_id,
            source.public_id AS source_public_id,
            source.source_code
        FROM iam.service_credential credential
        JOIN iam.app_user user_account
          ON user_account.id = credential.service_user_id
        JOIN integration.service_principal_source mapping
          ON mapping.service_user_id = user_account.id
        JOIN integration.ingestion_source source
          ON source.id = mapping.source_id
        WHERE credential.key_id = p_key_id
          AND credential.secret_hash = p_secret_hash
          AND credential.revoked_at IS NULL
          AND (credential.expires_at IS NULL
               OR credential.expires_at > clock_timestamp())
          AND user_account.enabled
          AND user_account.principal_kind = 'SERVICE'
          AND user_account.role = 'TEST_EXECUTOR'
          AND source.status = 'ACTIVE'
        LIMIT 1
    ), touched AS (
        UPDATE iam.service_credential credential
        SET last_used_at = clock_timestamp()
        FROM resolved
        WHERE credential.id = resolved.credential_id
        RETURNING credential.id
    )
    SELECT
        resolved.user_public_id,
        resolved.username,
        resolved.display_name,
        resolved.role,
        resolved.principal_kind,
        resolved.credential_public_id,
        resolved.source_public_id,
        resolved.source_code
    FROM resolved
    JOIN touched ON true;
END
$$;

CREATE OR REPLACE FUNCTION iam.admin_provision_service_credential(
    p_username text,
    p_display_name text,
    p_source_code text,
    p_key_id text,
    p_secret_hash text,
    p_campaign_public_ids uuid[],
    p_expires_at timestamptz,
    p_request_id text,
    p_source_ip inet
)
RETURNS TABLE (
    service_public_id uuid,
    credential_public_id uuid,
    source_public_id uuid
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, iam, integration, test, audit, public
AS $$
DECLARE
    v_username text := lower(btrim(coalesce(p_username, '')));
    v_display_name text := btrim(coalesce(p_display_name, ''));
    v_source_code text := upper(btrim(coalesce(p_source_code, '')));
    v_service_user_id bigint;
    v_source_id bigint;
    v_service_public_id uuid;
    v_credential_public_id uuid;
    v_source_public_id uuid;
BEGIN
    IF NOT iam.is_system_admin() THEN
        RAISE EXCEPTION 'administrator role required' USING ERRCODE = '42501';
    END IF;
    IF v_username !~ '^[a-z0-9][a-z0-9._-]{0,127}$'
       OR v_display_name = ''
       OR v_source_code !~ '^[A-Z0-9]+(-[A-Z0-9]+)*$'
       OR p_key_id !~ '^[A-Za-z0-9_-]{12,64}$'
       OR p_secret_hash !~ '^[0-9a-f]{64}$'
       OR coalesce(array_length(p_campaign_public_ids, 1), 0) = 0
       OR (p_expires_at IS NOT NULL AND p_expires_at <= clock_timestamp()) THEN
        RAISE EXCEPTION 'invalid service credential parameters'
            USING ERRCODE = '22023';
    END IF;

    INSERT INTO iam.app_user(
        username, display_name, role, principal_kind,
        enabled, must_change_password
    ) VALUES (
        v_username, v_display_name, 'TEST_EXECUTOR', 'SERVICE', true, false
    )
    RETURNING id, public_id
    INTO v_service_user_id, v_service_public_id;

    INSERT INTO integration.ingestion_source(
        source_code, source_type, direction, status, config_reference
    ) VALUES (
        v_source_code, 'API_COLLECTOR', 'INBOUND', 'ACTIVE',
        'service-principal:' || v_username
    )
    RETURNING id, public_id INTO v_source_id, v_source_public_id;

    INSERT INTO integration.service_principal_source(service_user_id, source_id)
    VALUES (v_service_user_id, v_source_id);

    IF EXISTS (
        SELECT 1
        FROM unnest(p_campaign_public_ids) requested(public_id)
        LEFT JOIN test.test_campaign campaign
          ON campaign.public_id = requested.public_id
         AND campaign.status IN ('PLANNED', 'ACTIVE', 'PAUSED')
        WHERE campaign.id IS NULL
    ) THEN
        RAISE EXCEPTION 'one or more campaigns do not exist or cannot ingest'
            USING ERRCODE = '22023';
    END IF;

    INSERT INTO iam.user_campaign_access(
        user_id, campaign_id, access_level, granted_by
    )
    SELECT
        v_service_user_id, campaign.id, 'EDIT', iam.current_user_id()
    FROM test.test_campaign campaign
    WHERE campaign.public_id = ANY(p_campaign_public_ids);

    INSERT INTO iam.service_credential(
        service_user_id, key_id, secret_hash, expires_at
    ) VALUES (
        v_service_user_id, p_key_id, p_secret_hash, p_expires_at
    )
    RETURNING public_id INTO v_credential_public_id;

    INSERT INTO audit.security_event(
        event_type, actor_user_id, username, outcome,
        request_id, source_ip, metadata
    ) VALUES (
        'SERVICE_CREDENTIAL_CREATE', iam.current_user_id(), v_username, 'SUCCESS',
        coalesce(p_request_id, ''), p_source_ip,
        jsonb_build_object(
            'service_public_id', v_service_public_id,
            'credential_public_id', v_credential_public_id,
            'source_public_id', v_source_public_id,
            'key_id', p_key_id,
            'campaign_count', array_length(p_campaign_public_ids, 1)
        )
    );

    RETURN QUERY SELECT
        v_service_public_id, v_credential_public_id, v_source_public_id;
END
$$;

CREATE OR REPLACE FUNCTION iam.admin_rotate_service_credential(
    p_service_public_id uuid,
    p_key_id text,
    p_secret_hash text,
    p_revoke_previous boolean,
    p_expires_at timestamptz,
    p_reason text,
    p_request_id text,
    p_source_ip inet
)
RETURNS uuid
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, iam, audit
AS $$
DECLARE
    v_service_user iam.app_user%ROWTYPE;
    v_previous_id bigint;
    v_credential_public_id uuid;
BEGIN
    IF NOT iam.is_system_admin() OR btrim(coalesce(p_reason, '')) = '' THEN
        RAISE EXCEPTION 'administrator role and reason required'
            USING ERRCODE = '42501';
    END IF;
    IF p_key_id !~ '^[A-Za-z0-9_-]{12,64}$'
       OR p_secret_hash !~ '^[0-9a-f]{64}$'
       OR (p_expires_at IS NOT NULL AND p_expires_at <= clock_timestamp()) THEN
        RAISE EXCEPTION 'invalid service credential parameters'
            USING ERRCODE = '22023';
    END IF;

    SELECT * INTO v_service_user
    FROM iam.app_user
    WHERE public_id = p_service_public_id
      AND principal_kind = 'SERVICE'
      AND enabled
    FOR UPDATE;
    IF v_service_user.id IS NULL THEN
        RAISE EXCEPTION 'service principal not found' USING ERRCODE = 'P0002';
    END IF;

    SELECT id INTO v_previous_id
    FROM iam.service_credential
    WHERE service_user_id = v_service_user.id
      AND revoked_at IS NULL
    ORDER BY created_at DESC, id DESC
    LIMIT 1
    FOR UPDATE;

    INSERT INTO iam.service_credential(
        service_user_id, key_id, secret_hash, expires_at, rotated_from_id
    ) VALUES (
        v_service_user.id, p_key_id, p_secret_hash, p_expires_at, v_previous_id
    )
    RETURNING public_id INTO v_credential_public_id;

    IF coalesce(p_revoke_previous, true) THEN
        UPDATE iam.service_credential
        SET revoked_at = clock_timestamp(),
            revoked_by = iam.current_user_id(),
            revoke_reason = btrim(p_reason)
        WHERE service_user_id = v_service_user.id
          AND public_id <> v_credential_public_id
          AND revoked_at IS NULL;
    END IF;

    INSERT INTO audit.security_event(
        event_type, actor_user_id, username, outcome,
        request_id, source_ip, metadata
    ) VALUES (
        'SERVICE_CREDENTIAL_ROTATE', iam.current_user_id(),
        v_service_user.username, 'SUCCESS', coalesce(p_request_id, ''),
        p_source_ip,
        jsonb_build_object(
            'service_public_id', p_service_public_id,
            'credential_public_id', v_credential_public_id,
            'key_id', p_key_id,
            'previous_revoked', coalesce(p_revoke_previous, true),
            'reason', btrim(p_reason)
        )
    );
    RETURN v_credential_public_id;
END
$$;

CREATE OR REPLACE FUNCTION iam.admin_revoke_service_credentials(
    p_service_public_id uuid,
    p_disable_principal boolean,
    p_reason text,
    p_request_id text,
    p_source_ip inet
)
RETURNS integer
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, iam, audit
AS $$
DECLARE
    v_service_user iam.app_user%ROWTYPE;
    v_revoked integer;
BEGIN
    IF NOT iam.is_system_admin() OR btrim(coalesce(p_reason, '')) = '' THEN
        RAISE EXCEPTION 'administrator role and reason required'
            USING ERRCODE = '42501';
    END IF;
    SELECT * INTO v_service_user
    FROM iam.app_user
    WHERE public_id = p_service_public_id
      AND principal_kind = 'SERVICE'
    FOR UPDATE;
    IF v_service_user.id IS NULL THEN
        RAISE EXCEPTION 'service principal not found' USING ERRCODE = 'P0002';
    END IF;

    UPDATE iam.service_credential
    SET revoked_at = clock_timestamp(),
        revoked_by = iam.current_user_id(),
        revoke_reason = btrim(p_reason)
    WHERE service_user_id = v_service_user.id
      AND revoked_at IS NULL;
    GET DIAGNOSTICS v_revoked = ROW_COUNT;

    IF coalesce(p_disable_principal, false) THEN
        UPDATE iam.app_user
        SET enabled = false, disabled_at = clock_timestamp()
        WHERE id = v_service_user.id;
    END IF;

    INSERT INTO audit.security_event(
        event_type, actor_user_id, username, outcome,
        request_id, source_ip, metadata
    ) VALUES (
        'SERVICE_CREDENTIAL_REVOKE', iam.current_user_id(),
        v_service_user.username, 'SUCCESS', coalesce(p_request_id, ''),
        p_source_ip,
        jsonb_build_object(
            'service_public_id', p_service_public_id,
            'credential_count', v_revoked,
            'principal_disabled', coalesce(p_disable_principal, false),
            'reason', btrim(p_reason)
        )
    );
    RETURN v_revoked;
END
$$;

CREATE OR REPLACE FUNCTION integration.validate_trajectory_links()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    v_campaign bigint;
    v_asset bigint;
    v_artifact_execution bigint;
BEGIN
    SELECT campaign_id, asset_id
    INTO v_campaign, v_asset
    FROM test.test_execution
    WHERE id = NEW.execution_id;
    IF ROW(v_campaign, v_asset)
       IS DISTINCT FROM ROW(NEW.campaign_id, NEW.asset_id) THEN
        RAISE EXCEPTION
            'trajectory campaign and asset do not match execution';
    END IF;
    IF NEW.artifact_id IS NOT NULL THEN
        SELECT execution_id INTO v_artifact_execution
        FROM integration.artifact
        WHERE id = NEW.artifact_id;
        IF v_artifact_execution IS DISTINCT FROM NEW.execution_id THEN
            RAISE EXCEPTION 'trajectory artifact does not match execution';
        END IF;
    END IF;
    RETURN NEW;
END
$$;

CREATE OR REPLACE FUNCTION test.validate_execution_ingestion_segment()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.ingestion_segment_id IS NOT NULL
       AND NOT EXISTS (
           SELECT 1
           FROM test.analysis_segment segment
           WHERE segment.id = NEW.ingestion_segment_id
             AND segment.campaign_id = NEW.campaign_id
             AND segment.cycle_id = NEW.cycle_id
             AND segment.configuration_snapshot_id =
                 NEW.configuration_snapshot_id
       ) THEN
        RAISE EXCEPTION
            'execution ingestion segment does not match campaign/cycle/configuration';
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER trg_validate_execution_ingestion_segment
BEFORE INSERT OR UPDATE OF
    campaign_id, cycle_id, configuration_snapshot_id, ingestion_segment_id
ON test.test_execution
FOR EACH ROW EXECUTE FUNCTION test.validate_execution_ingestion_segment();

CREATE TRIGGER trg_validate_trajectory_links
BEFORE INSERT OR UPDATE OF campaign_id, asset_id, execution_id, artifact_id
ON integration.trajectory_version
FOR EACH ROW EXECUTE FUNCTION integration.validate_trajectory_links();

CREATE TRIGGER trg_audit_row_change
AFTER INSERT OR UPDATE OR DELETE ON integration.service_principal_source
FOR EACH ROW EXECUTE FUNCTION audit.capture_row_change();
CREATE TRIGGER trg_audit_row_change
AFTER INSERT OR UPDATE OR DELETE ON integration.ingestion_batch
FOR EACH ROW EXECUTE FUNCTION audit.capture_row_change();
CREATE TRIGGER trg_audit_row_change
AFTER INSERT OR UPDATE OR DELETE ON integration.ingestion_receipt
FOR EACH ROW EXECUTE FUNCTION audit.capture_row_change();
CREATE TRIGGER trg_audit_row_change
AFTER INSERT OR UPDATE OR DELETE ON integration.trajectory_version
FOR EACH ROW EXECUTE FUNCTION audit.capture_row_change();

ALTER TABLE iam.service_credential ENABLE ROW LEVEL SECURITY;
CREATE POLICY service_credential_admin ON iam.service_credential
    FOR ALL USING (iam.is_system_admin()) WITH CHECK (iam.is_system_admin());

ALTER TABLE integration.service_principal_source ENABLE ROW LEVEL SECURITY;
CREATE POLICY service_principal_source_admin
    ON integration.service_principal_source
    FOR ALL USING (iam.is_system_admin()) WITH CHECK (iam.is_system_admin());
CREATE POLICY service_principal_source_self_select
    ON integration.service_principal_source
    FOR SELECT USING (
        service_user_id = iam.current_user_id()
        AND iam.current_principal_kind() = 'SERVICE'
    );

DROP POLICY IF EXISTS integration_admin ON integration.ingestion_source;
CREATE POLICY integration_source_admin ON integration.ingestion_source
    FOR ALL USING (iam.is_system_admin()) WITH CHECK (iam.is_system_admin());
CREATE POLICY ingestion_source_self_select ON integration.ingestion_source
    FOR SELECT USING (iam.can_ingest_source(id));

DROP POLICY IF EXISTS integration_admin ON integration.ingestion_receipt;
CREATE POLICY ingestion_receipt_admin ON integration.ingestion_receipt
    FOR ALL USING (iam.is_system_admin()) WITH CHECK (iam.is_system_admin());
CREATE POLICY ingestion_receipt_service ON integration.ingestion_receipt
    FOR ALL USING (iam.can_ingest_source(source_id))
    WITH CHECK (iam.can_ingest_source(source_id));

ALTER TABLE integration.ingestion_batch ENABLE ROW LEVEL SECURITY;
CREATE POLICY ingestion_batch_admin ON integration.ingestion_batch
    FOR ALL USING (iam.is_system_admin()) WITH CHECK (iam.is_system_admin());
CREATE POLICY ingestion_batch_service ON integration.ingestion_batch
    FOR ALL USING (iam.can_ingest_source(source_id))
    WITH CHECK (iam.can_ingest_source(source_id));

ALTER TABLE integration.trajectory_version ENABLE ROW LEVEL SECURITY;
CREATE POLICY trajectory_campaign_select ON integration.trajectory_version
    FOR SELECT USING (iam.can_access_campaign(campaign_id, false));
CREATE POLICY trajectory_campaign_write ON integration.trajectory_version
    FOR ALL USING (
        iam.can_access_campaign(campaign_id, true)
        AND iam.can_ingest_source(source_id)
    ) WITH CHECK (
        iam.can_access_campaign(campaign_id, true)
        AND iam.can_ingest_source(source_id)
    );

DROP POLICY IF EXISTS campaign_scoped_write ON integration.artifact;
CREATE POLICY artifact_campaign_write ON integration.artifact
    FOR ALL USING (
        iam.can_access_campaign(campaign_id, true)
        AND (
            iam.current_principal_kind() <> 'SERVICE'
            OR iam.can_ingest_source(source_id)
        )
    ) WITH CHECK (
        iam.can_access_campaign(campaign_id, true)
        AND (
            iam.current_principal_kind() <> 'SERVICE'
            OR iam.can_ingest_source(source_id)
        )
    );

GRANT SELECT, INSERT, UPDATE ON integration.service_principal_source,
    integration.ingestion_batch, integration.trajectory_version
TO __RP1_APP_USER__;
GRANT SELECT ON integration.service_principal_source,
    integration.ingestion_batch, integration.trajectory_version
TO __RP1_READONLY_USER__;
GRANT USAGE, SELECT ON SEQUENCE
    integration.ingestion_batch_id_seq,
    integration.trajectory_version_id_seq
TO __RP1_APP_USER__;

GRANT SELECT, INSERT, UPDATE ON integration.ingestion_receipt
TO __RP1_APP_USER__;
GRANT USAGE, SELECT ON SEQUENCE integration.ingestion_receipt_id_seq
TO __RP1_APP_USER__;

GRANT EXECUTE ON FUNCTION
    iam.current_ingestion_source_id(),
    iam.can_ingest_source(bigint)
TO __RP1_APP_USER__, __RP1_READONLY_USER__;
GRANT EXECUTE ON FUNCTION iam.auth_resolve_service_credential(text, text)
TO __RP1_APP_USER__;
GRANT EXECUTE ON FUNCTION iam.admin_provision_service_credential(
    text, text, text, text, text, uuid[], timestamptz, text, inet
) TO __RP1_APP_USER__;
GRANT EXECUTE ON FUNCTION iam.admin_rotate_service_credential(
    uuid, text, text, boolean, timestamptz, text, text, inet
) TO __RP1_APP_USER__;
GRANT EXECUTE ON FUNCTION iam.admin_revoke_service_credentials(
    uuid, boolean, text, text, inet
) TO __RP1_APP_USER__;

REVOKE ALL ON iam.service_credential FROM __RP1_APP_USER__, __RP1_READONLY_USER__;
REVOKE ALL ON FUNCTION iam.auth_resolve_service_credential(text, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION iam.admin_provision_service_credential(
    text, text, text, text, text, uuid[], timestamptz, text, inet
) FROM PUBLIC;
REVOKE ALL ON FUNCTION iam.admin_rotate_service_credential(
    uuid, text, text, boolean, timestamptz, text, text, inet
) FROM PUBLIC;
REVOKE ALL ON FUNCTION iam.admin_revoke_service_credentials(
    uuid, boolean, text, text, inet
) FROM PUBLIC;

COMMENT ON TABLE iam.service_credential IS
    'Rotatable service API credentials; only keyed irreversible hashes are stored.';
COMMENT ON TABLE integration.ingestion_batch IS
    'Producer-keyed batch lineage for idempotent edge collector ingestion.';
COMMENT ON TABLE integration.trajectory_version IS
    'Versioned trajectory file metadata and SHA-256 lineage; file bytes remain external.';
COMMENT ON COLUMN integration.artifact.metadata IS
    'Manifest/evidence metadata only; external object storage is not provided by compose.';
