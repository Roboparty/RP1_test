-- Browser account authentication and Campaign access workflow.
-- Authentication secrets remain behind narrowly-scoped SECURITY DEFINER functions.

ALTER TABLE iam.app_user
    DROP CONSTRAINT IF EXISTS app_user_username_normalized_check;
ALTER TABLE iam.app_user
    ADD CONSTRAINT app_user_username_normalized_check
    CHECK (
        username = lower(btrim(username))
        AND username ~ '^[a-z0-9][a-z0-9._-]{0,127}$'
    );

CREATE TABLE IF NOT EXISTS iam.campaign_access_request (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    public_id uuid NOT NULL DEFAULT public.uuid_v7() UNIQUE,
    requester_id bigint NOT NULL REFERENCES iam.app_user(id) ON DELETE CASCADE,
    campaign_id bigint NOT NULL REFERENCES test.test_campaign(id) ON DELETE CASCADE,
    requested_level text NOT NULL CHECK (requested_level IN ('VIEW', 'EDIT')),
    reason text NOT NULL CHECK (btrim(reason) <> ''),
    status text NOT NULL DEFAULT 'PENDING'
        CHECK (status IN ('PENDING', 'APPROVED', 'REJECTED', 'CANCELLED')),
    decided_by bigint REFERENCES iam.app_user(id),
    decided_at timestamptz,
    decision_reason text,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT campaign_access_request_decision_check CHECK (
        (
            status IN ('PENDING', 'CANCELLED')
            AND decided_by IS NULL
            AND decided_at IS NULL
            AND decision_reason IS NULL
        )
        OR (
            status IN ('APPROVED', 'REJECTED')
            AND decided_by IS NOT NULL
            AND decided_at IS NOT NULL
            AND btrim(coalesce(decision_reason, '')) <> ''
        )
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_campaign_access_request_pending
    ON iam.campaign_access_request(requester_id, campaign_id)
    WHERE status = 'PENDING';
CREATE INDEX IF NOT EXISTS idx_campaign_access_request_admin_queue
    ON iam.campaign_access_request(status, created_at, id);
CREATE INDEX IF NOT EXISTS idx_campaign_access_request_requester
    ON iam.campaign_access_request(requester_id, created_at DESC);

CREATE TABLE IF NOT EXISTS audit.security_event (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    public_id uuid NOT NULL DEFAULT public.uuid_v7() UNIQUE,
    event_type text NOT NULL CHECK (event_type ~ '^[A-Z][A-Z0-9_]{1,63}$'),
    actor_user_id bigint REFERENCES iam.app_user(id),
    username text NOT NULL DEFAULT '',
    outcome text NOT NULL CHECK (outcome IN ('SUCCESS', 'FAILURE', 'DENIED')),
    request_id text NOT NULL DEFAULT '',
    source_ip inet,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(metadata) = 'object'),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

COMMENT ON TABLE audit.security_event IS
'Append-only authentication and authorization security events. event_type is a documented uppercase snake-case application event such as LOGIN_SUCCESS, LOGIN_FAILURE, LOGOUT, PASSWORD_CHANGE, USER_CREATE, USER_DISABLE, ACCESS_REQUEST_SUBMIT, ACCESS_REQUEST_APPROVE, ACCESS_REQUEST_REJECT, ACCESS_REQUEST_CANCEL, CAMPAIGN_GRANT, or CAMPAIGN_REVOKE.';

CREATE INDEX IF NOT EXISTS idx_security_event_created
    ON audit.security_event(created_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_security_event_actor_created
    ON audit.security_event(actor_user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_security_event_type_created
    ON audit.security_event(event_type, created_at DESC);

DROP TRIGGER IF EXISTS trg_campaign_access_request_updated_at
    ON iam.campaign_access_request;
CREATE TRIGGER trg_campaign_access_request_updated_at
BEFORE UPDATE ON iam.campaign_access_request
FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();

DROP TRIGGER IF EXISTS trg_audit_row_change ON iam.campaign_access_request;
CREATE TRIGGER trg_audit_row_change
AFTER INSERT OR UPDATE OR DELETE ON iam.campaign_access_request
FOR EACH ROW EXECUTE FUNCTION audit.capture_row_change();

CREATE OR REPLACE FUNCTION audit.reject_security_event_mutation()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, audit
AS $$
BEGIN
    RAISE EXCEPTION 'audit.security_event is append-only' USING ERRCODE = '55000';
END;
$$;

DROP TRIGGER IF EXISTS trg_security_event_no_update ON audit.security_event;
CREATE TRIGGER trg_security_event_no_update
BEFORE UPDATE ON audit.security_event
FOR EACH ROW EXECUTE FUNCTION audit.reject_security_event_mutation();

DROP TRIGGER IF EXISTS trg_security_event_no_delete ON audit.security_event;
CREATE TRIGGER trg_security_event_no_delete
BEFORE DELETE ON audit.security_event
FOR EACH ROW EXECUTE FUNCTION audit.reject_security_event_mutation();

CREATE OR REPLACE FUNCTION audit.append_security_event(
    p_event_type text,
    p_username text,
    p_outcome text,
    p_request_id text,
    p_source_ip inet,
    p_metadata jsonb DEFAULT '{}'::jsonb,
    p_actor_public_id uuid DEFAULT NULL
)
RETURNS uuid
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, iam, audit, public
AS $$
DECLARE
    v_actor_id bigint := iam.current_user_id();
    v_event_id uuid;
BEGIN
    IF p_event_type !~ '^[A-Z][A-Z0-9_]{1,63}$'
       OR p_outcome NOT IN ('SUCCESS', 'FAILURE', 'DENIED')
       OR jsonb_typeof(coalesce(p_metadata, '{}'::jsonb)) <> 'object' THEN
        RAISE EXCEPTION 'invalid security event' USING ERRCODE = '22023';
    END IF;

    IF v_actor_id IS NULL AND p_actor_public_id IS NOT NULL THEN
        RAISE EXCEPTION 'anonymous security events cannot assert an actor'
            USING ERRCODE = '42501';
    ELSIF v_actor_id IS NOT NULL AND p_actor_public_id IS NOT NULL
          AND NOT iam.is_system_admin()
          AND NOT EXISTS (
              SELECT 1 FROM iam.app_user
              WHERE id = v_actor_id AND public_id = p_actor_public_id
          ) THEN
        RAISE EXCEPTION 'security event actor mismatch' USING ERRCODE = '42501';
    END IF;

    INSERT INTO audit.security_event(
        event_type, actor_user_id, username, outcome,
        request_id, source_ip, metadata
    ) VALUES (
        p_event_type, v_actor_id, lower(btrim(coalesce(p_username, ''))), p_outcome,
        coalesce(p_request_id, ''), p_source_ip, coalesce(p_metadata, '{}'::jsonb)
    )
    RETURNING public_id INTO v_event_id;
    RETURN v_event_id;
END;
$$;

CREATE OR REPLACE FUNCTION iam.bootstrap_initial_admin(
    p_username text,
    p_display_name text,
    p_argon2_hash text,
    p_request_id text,
    p_source_ip inet
)
RETURNS boolean
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
DECLARE
    v_user iam.app_user%ROWTYPE;
    v_username text := lower(btrim(coalesce(p_username, '')));
    v_display_name text := btrim(coalesce(p_display_name, ''));
BEGIN
    PERFORM pg_catalog.pg_advisory_xact_lock(
        pg_catalog.hashtextextended('rp1.iam.bootstrap-initial-admin', 0)
    );

    IF v_username = ''
       OR v_username <> coalesce(p_username, '')
       OR v_username !~ '^[a-z0-9][a-z0-9._-]{0,127}$'
       OR v_display_name = ''
       OR length(v_display_name) > 255
       OR p_argon2_hash !~ '^\$argon2id\$v=[0-9]+\$m=[0-9]+,t=[0-9]+,p=[0-9]+\$[A-Za-z0-9+/]+\$[A-Za-z0-9+/]+$'
       OR length(p_argon2_hash) > 512 THEN
        RAISE EXCEPTION 'invalid initial administrator parameters'
            USING ERRCODE = '22023';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM iam.user_credential c
        JOIN iam.app_user u ON u.id = c.user_id
        WHERE u.principal_kind = 'HUMAN'
    ) THEN
        RETURN false;
    END IF;

    SELECT * INTO v_user
    FROM iam.app_user
    WHERE username = v_username
    FOR UPDATE;

    IF v_user.id IS NOT NULL AND v_user.principal_kind <> 'HUMAN' THEN
        RAISE EXCEPTION 'bootstrap username belongs to a non-human principal'
            USING ERRCODE = '42501';
    END IF;

    IF v_user.id IS NULL THEN
        INSERT INTO iam.app_user(
            username, display_name, role, principal_kind,
            enabled, must_change_password
        ) VALUES (
            v_username, v_display_name, 'SYSTEM_ADMIN', 'HUMAN', true, true
        )
        RETURNING * INTO v_user;
    ELSE
        UPDATE iam.app_user
        SET display_name = v_display_name,
            role = 'SYSTEM_ADMIN',
            enabled = true,
            must_change_password = true,
            disabled_at = NULL,
            updated_at = pg_catalog.clock_timestamp()
        WHERE id = v_user.id
        RETURNING * INTO v_user;
    END IF;

    INSERT INTO iam.user_credential(
        user_id, password_hash, hash_algorithm, password_changed_at
    ) VALUES (
        v_user.id, p_argon2_hash, 'argon2id', pg_catalog.clock_timestamp()
    );

    INSERT INTO audit.security_event(
        event_type, actor_user_id, username, outcome,
        request_id, source_ip, metadata
    ) VALUES (
        'INITIAL_ADMIN_BOOTSTRAP', NULL, v_username, 'SUCCESS',
        coalesce(p_request_id, ''), p_source_ip,
        pg_catalog.jsonb_build_object('user_public_id', v_user.public_id)
    );
    RETURN true;
END;
$$;

CREATE OR REPLACE FUNCTION iam.auth_lookup_login_credential(p_username text)
RETURNS TABLE (
    user_public_id uuid,
    normalized_username text,
    display_name text,
    role text,
    principal_kind text,
    enabled boolean,
    must_change_password boolean,
    password_hash text,
    failed_attempts integer,
    locked_until timestamptz
)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, iam
AS $$
    SELECT
        u.public_id,
        u.username,
        u.display_name,
        u.role,
        u.principal_kind,
        u.enabled,
        u.must_change_password,
        c.password_hash,
        c.failed_attempts,
        c.locked_until
    FROM iam.app_user u
    JOIN iam.user_credential c ON c.user_id = u.id
    WHERE u.username = lower(btrim(p_username))
    LIMIT 1
$$;

CREATE OR REPLACE FUNCTION iam.auth_record_login_failure(
    p_username text,
    p_request_id text,
    p_source_ip inet
)
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, iam, audit
AS $$
DECLARE
    v_user iam.app_user%ROWTYPE;
    v_attempts integer;
    v_locked_until timestamptz;
BEGIN
    SELECT * INTO v_user
    FROM iam.app_user
    WHERE username = lower(btrim(p_username))
    LIMIT 1;

    IF v_user.id IS NOT NULL AND v_user.principal_kind = 'HUMAN' AND v_user.enabled THEN
        UPDATE iam.user_credential
        SET failed_attempts = failed_attempts + 1,
            locked_until = CASE
                WHEN failed_attempts + 1 >= 5 THEN clock_timestamp() + interval '15 minutes'
                ELSE locked_until
            END,
            updated_at = clock_timestamp()
        WHERE user_id = v_user.id
          AND (locked_until IS NULL OR locked_until <= clock_timestamp())
        RETURNING failed_attempts, locked_until INTO v_attempts, v_locked_until;
    END IF;

    INSERT INTO audit.security_event(
        event_type, actor_user_id, username, outcome,
        request_id, source_ip, metadata
    ) VALUES (
        'LOGIN_FAILURE',
        v_user.id,
        lower(btrim(coalesce(p_username, ''))),
        'FAILURE',
        coalesce(p_request_id, ''),
        p_source_ip,
        jsonb_build_object(
            'failed_attempts', coalesce(v_attempts, 0),
            'locked_until', v_locked_until
        )
    );
END;
$$;

CREATE OR REPLACE FUNCTION iam.auth_record_login_success(
    p_user_public_id uuid,
    p_request_id text,
    p_source_ip inet,
    p_rehash text DEFAULT NULL
)
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, iam, audit
AS $$
DECLARE
    v_user iam.app_user%ROWTYPE;
BEGIN
    IF p_rehash IS NOT NULL AND p_rehash NOT LIKE '$argon2id$%' THEN
        RAISE EXCEPTION 'invalid Argon2id rehash' USING ERRCODE = '22023';
    END IF;
    SELECT * INTO v_user
    FROM iam.app_user
    WHERE public_id = p_user_public_id
      AND enabled
      AND principal_kind = 'HUMAN'
    FOR UPDATE;

    IF v_user.id IS NULL OR EXISTS (
        SELECT 1 FROM iam.user_credential
        WHERE user_id = v_user.id AND locked_until > clock_timestamp()
    ) THEN
        RAISE EXCEPTION 'invalid login principal' USING ERRCODE = '28000';
    END IF;

    UPDATE iam.user_credential
    SET failed_attempts = 0,
        locked_until = NULL,
        password_hash = coalesce(nullif(p_rehash, ''), password_hash),
        hash_algorithm = 'argon2id',
        updated_at = clock_timestamp()
    WHERE user_id = v_user.id;

    INSERT INTO audit.security_event(
        event_type, actor_user_id, username, outcome,
        request_id, source_ip, metadata
    ) VALUES (
        'LOGIN_SUCCESS', v_user.id, v_user.username, 'SUCCESS',
        coalesce(p_request_id, ''), p_source_ip, '{}'::jsonb
    );
END;
$$;

CREATE OR REPLACE FUNCTION iam.auth_create_browser_session(
    p_user_public_id uuid,
    p_token_hash text,
    p_csrf_token_hash text,
    p_expires_at timestamptz
)
RETURNS TABLE (session_public_id uuid, expires_at timestamptz)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, iam
AS $$
DECLARE
    v_user_id bigint;
BEGIN
    IF length(p_token_hash) <> 64 OR length(p_csrf_token_hash) <> 64
       OR p_expires_at <= clock_timestamp()
       OR p_expires_at > clock_timestamp() + interval '24 hours' THEN
        RAISE EXCEPTION 'invalid browser session parameters' USING ERRCODE = '22023';
    END IF;

    SELECT u.id INTO v_user_id
    FROM iam.app_user u
    JOIN iam.user_credential c ON c.user_id = u.id
    WHERE u.public_id = p_user_public_id
      AND u.enabled
      AND u.principal_kind = 'HUMAN'
      AND (c.locked_until IS NULL OR c.locked_until <= clock_timestamp())
    FOR UPDATE OF u, c;

    IF v_user_id IS NULL THEN
        RAISE EXCEPTION 'invalid login principal' USING ERRCODE = '28000';
    END IF;

    RETURN QUERY
    INSERT INTO iam.auth_session AS s(
        user_id, token_hash, csrf_token_hash, session_type, expires_at
    ) VALUES (
        v_user_id, p_token_hash, p_csrf_token_hash, 'BROWSER', p_expires_at
    )
    RETURNING s.public_id, s.expires_at;
END;
$$;

CREATE OR REPLACE FUNCTION iam.auth_resolve_browser_session(p_token_hash text)
RETURNS TABLE (
    session_public_id uuid,
    user_public_id uuid,
    username text,
    display_name text,
    role text,
    principal_kind text,
    must_change_password boolean,
    session_type text,
    csrf_token_hash text,
    expires_at timestamptz
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, iam
AS $$
BEGIN
    RETURN QUERY
    WITH resolved AS (
        SELECT
            s.id,
            s.public_id AS session_public_id,
            u.public_id AS user_public_id,
            u.username,
            u.display_name,
            u.role,
            u.principal_kind,
            u.must_change_password,
            s.session_type,
            s.csrf_token_hash,
            s.expires_at
        FROM iam.auth_session s
        JOIN iam.app_user u ON u.id = s.user_id
        JOIN iam.user_credential c ON c.user_id = u.id
        WHERE s.token_hash = p_token_hash
          AND s.session_type = 'BROWSER'
          AND s.revoked_at IS NULL
          AND s.expires_at > clock_timestamp()
          AND u.enabled
          AND u.principal_kind = 'HUMAN'
          AND (c.locked_until IS NULL OR c.locked_until <= clock_timestamp())
        LIMIT 1
    ), touched AS (
        UPDATE iam.auth_session s
        SET last_seen_at = clock_timestamp()
        FROM resolved r
        WHERE s.id = r.id
        RETURNING s.id
    )
    SELECT
        r.session_public_id,
        r.user_public_id,
        r.username,
        r.display_name,
        r.role,
        r.principal_kind,
        r.must_change_password,
        r.session_type,
        r.csrf_token_hash,
        r.expires_at
    FROM resolved r
    JOIN touched t ON t.id = r.id;
END;
$$;

CREATE OR REPLACE FUNCTION iam.auth_revoke_browser_session(
    p_session_public_id uuid,
    p_request_id text,
    p_source_ip inet
)
RETURNS boolean
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, iam, audit
AS $$
DECLARE
    v_username text;
BEGIN
    UPDATE iam.auth_session s
    SET revoked_at = coalesce(s.revoked_at, clock_timestamp())
    FROM iam.app_user u
    WHERE s.public_id = p_session_public_id
      AND s.user_id = iam.current_user_id()
      AND s.user_id = u.id
      AND s.session_type = 'BROWSER'
      AND s.revoked_at IS NULL
    RETURNING u.username INTO v_username;

    IF v_username IS NULL THEN
        RETURN false;
    END IF;

    INSERT INTO audit.security_event(
        event_type, actor_user_id, username, outcome,
        request_id, source_ip, metadata
    ) VALUES (
        'LOGOUT', iam.current_user_id(), v_username, 'SUCCESS',
        coalesce(p_request_id, ''), p_source_ip,
        jsonb_build_object('session_public_id', p_session_public_id)
    );
    RETURN true;
END;
$$;

CREATE OR REPLACE FUNCTION iam.auth_change_current_password(
    p_session_public_id uuid,
    p_new_password_hash text,
    p_request_id text,
    p_source_ip inet
)
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, iam, audit
AS $$
DECLARE
    v_user iam.app_user%ROWTYPE;
    v_session_id bigint;
BEGIN
    SELECT u.* INTO v_user
    FROM iam.app_user u
    JOIN iam.auth_session s ON s.user_id = u.id
    WHERE u.id = iam.current_user_id()
      AND s.public_id = p_session_public_id
      AND s.session_type = 'BROWSER'
      AND s.revoked_at IS NULL
      AND s.expires_at > clock_timestamp()
    FOR UPDATE OF u, s;

    IF v_user.id IS NULL OR v_user.principal_kind <> 'HUMAN' OR NOT v_user.enabled
       OR p_new_password_hash NOT LIKE '$argon2id$%' THEN
        RAISE EXCEPTION 'password change denied' USING ERRCODE = '42501';
    END IF;

    SELECT id INTO v_session_id
    FROM iam.auth_session
    WHERE public_id = p_session_public_id;

    UPDATE iam.user_credential
    SET password_hash = p_new_password_hash,
        hash_algorithm = 'argon2id',
        password_changed_at = clock_timestamp(),
        failed_attempts = 0,
        locked_until = NULL,
        updated_at = clock_timestamp()
    WHERE user_id = v_user.id;

    UPDATE iam.app_user
    SET must_change_password = false,
        updated_at = clock_timestamp()
    WHERE id = v_user.id;

    UPDATE iam.auth_session
    SET revoked_at = clock_timestamp()
    WHERE user_id = v_user.id
      AND id <> v_session_id
      AND revoked_at IS NULL;

    INSERT INTO audit.security_event(
        event_type, actor_user_id, username, outcome,
        request_id, source_ip, metadata
    ) VALUES (
        'PASSWORD_CHANGE', v_user.id, v_user.username, 'SUCCESS',
        coalesce(p_request_id, ''), p_source_ip,
        jsonb_build_object('other_sessions_revoked', true)
    );
END;
$$;

CREATE OR REPLACE FUNCTION iam.admin_list_users()
RETURNS TABLE (
    public_id uuid,
    username text,
    display_name text,
    role text,
    principal_kind text,
    enabled boolean,
    must_change_password boolean,
    created_at timestamptz,
    disabled_at timestamptz
)
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, iam
AS $$
BEGIN
    IF NOT iam.is_system_admin() THEN
        RAISE EXCEPTION 'administrator role required' USING ERRCODE = '42501';
    END IF;
    RETURN QUERY
    SELECT
        u.public_id, u.username, u.display_name, u.role, u.principal_kind,
        u.enabled, u.must_change_password, u.created_at, u.disabled_at
    FROM iam.app_user u
    ORDER BY u.username;
END;
$$;

CREATE OR REPLACE FUNCTION iam.admin_create_user(
    p_username text,
    p_display_name text,
    p_role text,
    p_password_hash text,
    p_must_change_password boolean,
    p_request_id text,
    p_source_ip inet
)
RETURNS uuid
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, iam, audit, public
AS $$
DECLARE
    v_user_id bigint;
    v_public_id uuid;
    v_username text := lower(btrim(p_username));
BEGIN
    IF NOT iam.is_system_admin() THEN
        RAISE EXCEPTION 'administrator role required' USING ERRCODE = '42501';
    END IF;
    IF v_username = '' OR p_role NOT IN ('VIEWER', 'TEST_EXECUTOR', 'SYSTEM_ADMIN')
       OR p_password_hash NOT LIKE '$argon2id$%' THEN
        RAISE EXCEPTION 'invalid user parameters' USING ERRCODE = '22023';
    END IF;

    INSERT INTO iam.app_user(
        username, display_name, role, principal_kind, enabled, must_change_password
    ) VALUES (
        v_username, btrim(p_display_name), p_role, 'HUMAN', true,
        coalesce(p_must_change_password, true)
    )
    RETURNING id, public_id INTO v_user_id, v_public_id;

    INSERT INTO iam.user_credential(user_id, password_hash, hash_algorithm)
    VALUES (v_user_id, p_password_hash, 'argon2id');

    INSERT INTO audit.security_event(
        event_type, actor_user_id, username, outcome,
        request_id, source_ip, metadata
    ) VALUES (
        'USER_CREATE', iam.current_user_id(), v_username, 'SUCCESS',
        coalesce(p_request_id, ''), p_source_ip,
        jsonb_build_object('user_public_id', v_public_id, 'role', p_role)
    );
    RETURN v_public_id;
END;
$$;

CREATE OR REPLACE FUNCTION iam.admin_disable_user(
    p_user_public_id uuid,
    p_reason text,
    p_request_id text,
    p_source_ip inet
)
RETURNS boolean
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, iam, audit
AS $$
DECLARE
    v_target iam.app_user%ROWTYPE;
BEGIN
    IF NOT iam.is_system_admin() OR btrim(coalesce(p_reason, '')) = '' THEN
        RAISE EXCEPTION 'administrator role and reason required' USING ERRCODE = '42501';
    END IF;
    SELECT * INTO v_target
    FROM iam.app_user
    WHERE public_id = p_user_public_id
    FOR UPDATE;
    IF v_target.id IS NULL THEN
        RETURN false;
    END IF;
    IF v_target.id = iam.current_user_id() THEN
        RAISE EXCEPTION 'administrator cannot disable self' USING ERRCODE = '42501';
    END IF;

    UPDATE iam.app_user
    SET enabled = false,
        disabled_at = coalesce(disabled_at, clock_timestamp()),
        updated_at = clock_timestamp()
    WHERE id = v_target.id;
    UPDATE iam.auth_session
    SET revoked_at = clock_timestamp()
    WHERE user_id = v_target.id AND revoked_at IS NULL;

    INSERT INTO audit.security_event(
        event_type, actor_user_id, username, outcome,
        request_id, source_ip, metadata
    ) VALUES (
        'USER_DISABLE', iam.current_user_id(), v_target.username, 'SUCCESS',
        coalesce(p_request_id, ''), p_source_ip,
        jsonb_build_object('user_public_id', p_user_public_id, 'reason', btrim(p_reason))
    );
    RETURN true;
END;
$$;

CREATE OR REPLACE FUNCTION iam.current_campaign_access()
RETURNS TABLE (
    campaign_public_id uuid,
    campaign_code text,
    campaign_name text,
    asset_kind text,
    access_level text,
    granted_at timestamptz
)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, iam, test
AS $$
    SELECT c.public_id, c.campaign_code, c.name, c.asset_kind,
           CASE WHEN iam.is_system_admin() THEN 'EDIT' ELSE a.access_level END,
           a.granted_at
    FROM test.test_campaign c
    LEFT JOIN iam.user_campaign_access a
      ON a.campaign_id = c.id AND a.user_id = iam.current_user_id()
    WHERE (iam.is_system_admin() AND c.status = 'ACTIVE')
       OR a.user_id IS NOT NULL
    ORDER BY c.campaign_code
$$;

CREATE OR REPLACE FUNCTION iam.list_requestable_active_campaigns()
RETURNS TABLE (
    campaign_public_id uuid,
    campaign_code text,
    campaign_name text,
    asset_kind text,
    current_grant text,
    pending_request_public_id uuid,
    pending_requested_level text
)
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, iam, test
AS $$
BEGIN
    IF NOT iam.is_authenticated() OR iam.current_principal_kind() <> 'HUMAN' THEN
        RAISE EXCEPTION 'human authentication required' USING ERRCODE = '42501';
    END IF;
    RETURN QUERY
    SELECT
        c.public_id, c.campaign_code, c.name, c.asset_kind,
        g.access_level, r.public_id, r.requested_level
    FROM test.test_campaign c
    LEFT JOIN iam.user_campaign_access g
      ON g.campaign_id = c.id AND g.user_id = iam.current_user_id()
    LEFT JOIN iam.campaign_access_request r
      ON r.campaign_id = c.id
     AND r.requester_id = iam.current_user_id()
     AND r.status = 'PENDING'
    WHERE c.status = 'ACTIVE'
    ORDER BY c.campaign_code;
END;
$$;

CREATE OR REPLACE FUNCTION iam.submit_campaign_access_request(
    p_campaign_public_id uuid,
    p_requested_level text,
    p_reason text,
    p_request_id text,
    p_source_ip inet
)
RETURNS uuid
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, iam, test, audit
AS $$
DECLARE
    v_campaign_id bigint;
    v_request_public_id uuid;
BEGIN
    IF NOT iam.is_authenticated() OR iam.current_principal_kind() <> 'HUMAN'
       OR p_requested_level NOT IN ('VIEW', 'EDIT')
       OR btrim(coalesce(p_reason, '')) = '' THEN
        RAISE EXCEPTION 'invalid access request' USING ERRCODE = '22023';
    END IF;
    SELECT id INTO v_campaign_id
    FROM test.test_campaign
    WHERE public_id = p_campaign_public_id AND status = 'ACTIVE';
    IF v_campaign_id IS NULL THEN
        RAISE EXCEPTION 'campaign not found or inactive' USING ERRCODE = 'P0002';
    END IF;
    IF EXISTS (
        SELECT 1
        FROM iam.user_campaign_access
        WHERE user_id = iam.current_user_id()
          AND campaign_id = v_campaign_id
          AND (
              access_level = 'EDIT'
              OR (access_level = 'VIEW' AND p_requested_level = 'VIEW')
          )
    ) THEN
        RAISE EXCEPTION 'requested access is already granted' USING ERRCODE = 'P0001';
    END IF;

    INSERT INTO iam.campaign_access_request(
        requester_id, campaign_id, requested_level, reason
    ) VALUES (
        iam.current_user_id(), v_campaign_id, p_requested_level, btrim(p_reason)
    )
    RETURNING public_id INTO v_request_public_id;

    INSERT INTO audit.security_event(
        event_type, actor_user_id, username, outcome,
        request_id, source_ip, metadata
    ) VALUES (
        'ACCESS_REQUEST_SUBMIT', iam.current_user_id(),
        (SELECT username FROM iam.app_user WHERE id = iam.current_user_id()),
        'SUCCESS',
        coalesce(p_request_id, ''), p_source_ip,
        jsonb_build_object(
            'access_request_public_id', v_request_public_id,
            'campaign_public_id', p_campaign_public_id,
            'requested_level', p_requested_level
        )
    );
    RETURN v_request_public_id;
END;
$$;

CREATE OR REPLACE FUNCTION iam.list_my_access_requests()
RETURNS TABLE (
    public_id uuid,
    campaign_public_id uuid,
    campaign_code text,
    campaign_name text,
    requested_level text,
    reason text,
    status text,
    decision_reason text,
    created_at timestamptz,
    updated_at timestamptz,
    decided_at timestamptz
)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, iam, test
AS $$
    SELECT
        r.public_id, c.public_id, c.campaign_code, c.name,
        r.requested_level, r.reason, r.status, r.decision_reason,
        r.created_at, r.updated_at, r.decided_at
    FROM iam.campaign_access_request r
    JOIN test.test_campaign c ON c.id = r.campaign_id
    WHERE r.requester_id = iam.current_user_id()
    ORDER BY r.created_at DESC, r.id DESC
$$;

CREATE OR REPLACE FUNCTION iam.cancel_campaign_access_request(
    p_access_request_public_id uuid,
    p_request_id text,
    p_source_ip inet
)
RETURNS boolean
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, iam, audit
AS $$
DECLARE
    v_level text;
BEGIN
    UPDATE iam.campaign_access_request
    SET status = 'CANCELLED', updated_at = clock_timestamp()
    WHERE public_id = p_access_request_public_id
      AND requester_id = iam.current_user_id()
      AND status = 'PENDING'
    RETURNING requested_level INTO v_level;
    IF v_level IS NULL THEN
        RETURN false;
    END IF;
    INSERT INTO audit.security_event(
        event_type, actor_user_id, username, outcome,
        request_id, source_ip, metadata
    ) VALUES (
        'ACCESS_REQUEST_CANCEL', iam.current_user_id(),
        (SELECT username FROM iam.app_user WHERE id = iam.current_user_id()),
        'SUCCESS',
        coalesce(p_request_id, ''), p_source_ip,
        jsonb_build_object('access_request_public_id', p_access_request_public_id)
    );
    RETURN true;
END;
$$;

CREATE OR REPLACE FUNCTION iam.admin_list_access_requests(p_status text DEFAULT NULL)
RETURNS TABLE (
    public_id uuid,
    requester_public_id uuid,
    requester_username text,
    campaign_public_id uuid,
    campaign_code text,
    campaign_name text,
    requested_level text,
    reason text,
    status text,
    decision_reason text,
    created_at timestamptz,
    updated_at timestamptz,
    decided_at timestamptz,
    decided_by_public_id uuid
)
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, iam, test
AS $$
BEGIN
    IF NOT iam.is_system_admin()
       OR (p_status IS NOT NULL AND p_status NOT IN ('PENDING', 'APPROVED', 'REJECTED', 'CANCELLED')) THEN
        RAISE EXCEPTION 'administrator role required or invalid status' USING ERRCODE = '42501';
    END IF;
    RETURN QUERY
    SELECT
        r.public_id, u.public_id, u.username, c.public_id, c.campaign_code, c.name,
        r.requested_level, r.reason, r.status, r.decision_reason,
        r.created_at, r.updated_at, r.decided_at, d.public_id
    FROM iam.campaign_access_request r
    JOIN iam.app_user u ON u.id = r.requester_id
    JOIN test.test_campaign c ON c.id = r.campaign_id
    LEFT JOIN iam.app_user d ON d.id = r.decided_by
    WHERE p_status IS NULL OR r.status = p_status
    ORDER BY r.created_at DESC, r.id DESC;
END;
$$;

CREATE OR REPLACE FUNCTION iam.admin_decide_campaign_access_request(
    p_access_request_public_id uuid,
    p_decision text,
    p_reason text,
    p_request_id text,
    p_source_ip inet
)
RETURNS boolean
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, iam, audit
AS $$
DECLARE
    v_request iam.campaign_access_request%ROWTYPE;
    v_event_type text;
BEGIN
    IF NOT iam.is_system_admin() OR p_decision NOT IN ('APPROVED', 'REJECTED')
       OR btrim(coalesce(p_reason, '')) = '' THEN
        RAISE EXCEPTION 'invalid access decision' USING ERRCODE = '42501';
    END IF;
    SELECT * INTO v_request
    FROM iam.campaign_access_request
    WHERE public_id = p_access_request_public_id
    FOR UPDATE;
    IF v_request.id IS NULL THEN
        RETURN false;
    END IF;
    IF v_request.status <> 'PENDING' THEN
        RAISE EXCEPTION 'access request is not pending' USING ERRCODE = '40001';
    END IF;
    IF v_request.requester_id = iam.current_user_id() THEN
        RAISE EXCEPTION 'administrator cannot decide own request' USING ERRCODE = '42501';
    END IF;

    IF p_decision = 'APPROVED' THEN
        INSERT INTO iam.user_campaign_access(
            user_id, campaign_id, access_level, granted_by, granted_at
        ) VALUES (
            v_request.requester_id, v_request.campaign_id,
            v_request.requested_level, iam.current_user_id(), clock_timestamp()
        )
        ON CONFLICT (user_id, campaign_id) DO UPDATE
        SET access_level = CASE
                WHEN iam.user_campaign_access.access_level = 'EDIT' THEN 'EDIT'
                ELSE EXCLUDED.access_level
            END,
            granted_by = EXCLUDED.granted_by,
            granted_at = EXCLUDED.granted_at;
        v_event_type := 'ACCESS_REQUEST_APPROVE';
    ELSE
        v_event_type := 'ACCESS_REQUEST_REJECT';
    END IF;

    UPDATE iam.campaign_access_request
    SET status = p_decision,
        decided_by = iam.current_user_id(),
        decided_at = clock_timestamp(),
        decision_reason = btrim(p_reason),
        updated_at = clock_timestamp()
    WHERE id = v_request.id;

    INSERT INTO audit.security_event(
        event_type, actor_user_id, username, outcome,
        request_id, source_ip, metadata
    ) VALUES (
        v_event_type, iam.current_user_id(),
        (SELECT username FROM iam.app_user WHERE id = iam.current_user_id()),
        'SUCCESS',
        coalesce(p_request_id, ''), p_source_ip,
        jsonb_build_object(
            'access_request_public_id', p_access_request_public_id,
            'requested_level', v_request.requested_level,
            'decision_reason', btrim(p_reason)
        )
    );
    RETURN true;
END;
$$;

CREATE OR REPLACE FUNCTION iam.admin_list_user_campaign_grants(p_user_public_id uuid)
RETURNS TABLE (
    campaign_public_id uuid,
    campaign_code text,
    campaign_name text,
    asset_kind text,
    access_level text,
    granted_at timestamptz,
    granted_by_public_id uuid
)
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, iam, test
AS $$
BEGIN
    IF NOT iam.is_system_admin() THEN
        RAISE EXCEPTION 'administrator role required' USING ERRCODE = '42501';
    END IF;
    RETURN QUERY
    SELECT c.public_id, c.campaign_code, c.name, c.asset_kind,
           g.access_level, g.granted_at, a.public_id
    FROM iam.user_campaign_access g
    JOIN iam.app_user u ON u.id = g.user_id
    JOIN iam.app_user a ON a.id = g.granted_by
    JOIN test.test_campaign c ON c.id = g.campaign_id
    WHERE u.public_id = p_user_public_id
    ORDER BY c.campaign_code;
END;
$$;

CREATE OR REPLACE FUNCTION iam.admin_set_user_campaign_grant(
    p_user_public_id uuid,
    p_campaign_public_id uuid,
    p_access_level text,
    p_reason text,
    p_request_id text,
    p_source_ip inet
)
RETURNS boolean
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, iam, test, audit
AS $$
DECLARE
    v_user_id bigint;
    v_campaign_id bigint;
BEGIN
    IF NOT iam.is_system_admin() OR p_access_level NOT IN ('VIEW', 'EDIT')
       OR btrim(coalesce(p_reason, '')) = '' THEN
        RAISE EXCEPTION 'invalid campaign grant' USING ERRCODE = '42501';
    END IF;
    SELECT id INTO v_user_id
    FROM iam.app_user
    WHERE public_id = p_user_public_id AND enabled AND principal_kind = 'HUMAN';
    SELECT id INTO v_campaign_id
    FROM test.test_campaign
    WHERE public_id = p_campaign_public_id;
    IF v_user_id IS NULL OR v_campaign_id IS NULL THEN
        RETURN false;
    END IF;

    INSERT INTO iam.user_campaign_access(
        user_id, campaign_id, access_level, granted_by, granted_at
    ) VALUES (
        v_user_id, v_campaign_id, p_access_level, iam.current_user_id(), clock_timestamp()
    )
    ON CONFLICT (user_id, campaign_id) DO UPDATE
    SET access_level = CASE
            WHEN iam.user_campaign_access.access_level = 'EDIT' THEN 'EDIT'
            ELSE EXCLUDED.access_level
        END,
        granted_by = EXCLUDED.granted_by,
        granted_at = EXCLUDED.granted_at;

    INSERT INTO audit.security_event(
        event_type, actor_user_id, username, outcome,
        request_id, source_ip, metadata
    ) VALUES (
        'CAMPAIGN_GRANT', iam.current_user_id(),
        (SELECT username FROM iam.app_user WHERE id = iam.current_user_id()),
        'SUCCESS',
        coalesce(p_request_id, ''), p_source_ip,
        jsonb_build_object(
            'user_public_id', p_user_public_id,
            'campaign_public_id', p_campaign_public_id,
            'access_level', p_access_level,
            'reason', btrim(p_reason)
        )
    );
    RETURN true;
END;
$$;

CREATE OR REPLACE FUNCTION iam.admin_revoke_user_campaign_grant(
    p_user_public_id uuid,
    p_campaign_public_id uuid,
    p_reason text,
    p_request_id text,
    p_source_ip inet
)
RETURNS boolean
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, iam, test, audit
AS $$
DECLARE
    v_deleted boolean;
BEGIN
    IF NOT iam.is_system_admin() OR btrim(coalesce(p_reason, '')) = '' THEN
        RAISE EXCEPTION 'administrator role and reason required' USING ERRCODE = '42501';
    END IF;
    DELETE FROM iam.user_campaign_access g
    USING iam.app_user u, test.test_campaign c
    WHERE g.user_id = u.id
      AND g.campaign_id = c.id
      AND u.public_id = p_user_public_id
      AND c.public_id = p_campaign_public_id;
    v_deleted := FOUND;
    IF v_deleted THEN
        INSERT INTO audit.security_event(
            event_type, actor_user_id, username, outcome,
            request_id, source_ip, metadata
        ) VALUES (
            'CAMPAIGN_REVOKE', iam.current_user_id(),
            (SELECT username FROM iam.app_user WHERE id = iam.current_user_id()),
            'SUCCESS',
            coalesce(p_request_id, ''), p_source_ip,
            jsonb_build_object(
                'user_public_id', p_user_public_id,
                'campaign_public_id', p_campaign_public_id,
                'reason', btrim(p_reason)
            )
        );
    END IF;
    RETURN v_deleted;
END;
$$;

-- Tighten identity visibility and preserve self/admin access semantics.
DROP POLICY IF EXISTS app_user_select ON iam.app_user;
CREATE POLICY app_user_select ON iam.app_user
FOR SELECT USING (id = iam.current_user_id() OR iam.is_system_admin());

ALTER TABLE iam.campaign_access_request ENABLE ROW LEVEL SECURITY;
ALTER TABLE iam.campaign_access_request FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS access_request_select ON iam.campaign_access_request;
CREATE POLICY access_request_select ON iam.campaign_access_request
FOR SELECT USING (requester_id = iam.current_user_id() OR iam.is_system_admin());
DROP POLICY IF EXISTS access_request_insert ON iam.campaign_access_request;
CREATE POLICY access_request_insert ON iam.campaign_access_request
FOR INSERT WITH CHECK (
    requester_id = iam.current_user_id()
    AND status = 'PENDING'
    AND decided_by IS NULL
    AND decided_at IS NULL
    AND decision_reason IS NULL
);
DROP POLICY IF EXISTS access_request_cancel ON iam.campaign_access_request;
CREATE POLICY access_request_cancel ON iam.campaign_access_request
FOR UPDATE USING (
    requester_id = iam.current_user_id() AND status = 'PENDING'
) WITH CHECK (
    requester_id = iam.current_user_id()
    AND status = 'CANCELLED'
    AND decided_by IS NULL
    AND decided_at IS NULL
    AND decision_reason IS NULL
);
DROP POLICY IF EXISTS access_request_admin ON iam.campaign_access_request;
CREATE POLICY access_request_admin ON iam.campaign_access_request
FOR ALL USING (iam.is_system_admin()) WITH CHECK (iam.is_system_admin());

ALTER TABLE audit.security_event ENABLE ROW LEVEL SECURITY;
ALTER TABLE audit.security_event FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS security_event_admin_select ON audit.security_event;
CREATE POLICY security_event_admin_select ON audit.security_event
FOR SELECT USING (iam.is_system_admin());
DROP POLICY IF EXISTS security_event_owner_insert ON audit.security_event;
CREATE POLICY security_event_owner_insert ON audit.security_event
FOR INSERT WITH CHECK (
    current_user = (
        SELECT pg_get_userbyid(c.relowner)
        FROM pg_class c
        WHERE c.oid = 'audit.security_event'::regclass
    )
);

GRANT SELECT, INSERT, UPDATE ON iam.campaign_access_request TO __RP1_APP_USER__;
GRANT USAGE, SELECT ON SEQUENCE iam.campaign_access_request_id_seq TO __RP1_APP_USER__;
GRANT SELECT ON audit.security_event TO __RP1_APP_USER__;

REVOKE ALL ON TABLE iam.user_credential, iam.auth_session
FROM __RP1_APP_USER__, __RP1_READONLY_USER__;
REVOKE INSERT, UPDATE, DELETE ON iam.user_campaign_access FROM __RP1_READONLY_USER__;
REVOKE INSERT, UPDATE, DELETE ON audit.security_event
FROM __RP1_APP_USER__, __RP1_READONLY_USER__, PUBLIC;
REVOKE UPDATE, DELETE ON audit.security_event FROM PUBLIC;
REVOKE ALL ON FUNCTION audit.reject_security_event_mutation() FROM PUBLIC;
REVOKE ALL ON FUNCTION iam.bootstrap_initial_admin(text,text,text,text,inet)
FROM PUBLIC, __RP1_READONLY_USER__;
GRANT EXECUTE ON FUNCTION iam.bootstrap_initial_admin(text,text,text,text,inet)
TO __RP1_APP_USER__;

DO $$
DECLARE
    v_signature text;
BEGIN
    FOREACH v_signature IN ARRAY ARRAY[
        'audit.append_security_event(text,text,text,text,inet,jsonb,uuid)',
        'iam.auth_lookup_login_credential(text)',
        'iam.auth_record_login_failure(text,text,inet)',
        'iam.auth_record_login_success(uuid,text,inet,text)',
        'iam.auth_create_browser_session(uuid,text,text,timestamptz)',
        'iam.auth_resolve_browser_session(text)',
        'iam.auth_revoke_browser_session(uuid,text,inet)',
        'iam.auth_change_current_password(uuid,text,text,inet)',
        'iam.admin_list_users()',
        'iam.admin_create_user(text,text,text,text,boolean,text,inet)',
        'iam.admin_disable_user(uuid,text,text,inet)',
        'iam.current_campaign_access()',
        'iam.list_requestable_active_campaigns()',
        'iam.submit_campaign_access_request(uuid,text,text,text,inet)',
        'iam.list_my_access_requests()',
        'iam.cancel_campaign_access_request(uuid,text,inet)',
        'iam.admin_list_access_requests(text)',
        'iam.admin_decide_campaign_access_request(uuid,text,text,text,inet)',
        'iam.admin_list_user_campaign_grants(uuid)',
        'iam.admin_set_user_campaign_grant(uuid,uuid,text,text,text,inet)',
        'iam.admin_revoke_user_campaign_grant(uuid,uuid,text,text,inet)'
    ] LOOP
        EXECUTE 'REVOKE ALL ON FUNCTION ' || v_signature || ' FROM PUBLIC';
        EXECUTE 'GRANT EXECUTE ON FUNCTION ' || v_signature
            || ' TO __RP1_APP_USER__, __RP1_MIGRATOR_USER__';
    END LOOP;
END;
$$;
