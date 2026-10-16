CREATE TABLE integration.hmi_presence (
    connection_id uuid PRIMARY KEY,
    installation_id uuid NOT NULL,
    user_id bigint NOT NULL REFERENCES iam.app_user(id),
    auth_session_id bigint NOT NULL REFERENCES iam.auth_session(id),
    station_name text NOT NULL CHECK (length(station_name) BETWEEN 1 AND 128),
    bench_id text NOT NULL DEFAULT '' CHECK (length(bench_id) <= 64),
    sequence bigint NOT NULL CHECK (sequence > 0),
    connected_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    last_seen_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    snapshot_at timestamptz NOT NULL,
    disconnected_at timestamptz,
    runs jsonb NOT NULL DEFAULT '[]' CHECK (jsonb_typeof(runs) = 'array' AND jsonb_array_length(runs) <= 32),
    plc jsonb NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(plc) = 'object')
);

CREATE INDEX ix_hmi_presence_installation ON integration.hmi_presence(installation_id, connected_at DESC, connection_id);
CREATE INDEX ix_hmi_presence_user ON integration.hmi_presence(user_id);
CREATE INDEX ix_hmi_presence_session ON integration.hmi_presence(auth_session_id);
ALTER TABLE integration.hmi_presence ENABLE ROW LEVEL SECURITY;
CREATE POLICY hmi_presence_read ON integration.hmi_presence FOR SELECT
    USING (user_id = iam.current_user_id() OR iam.is_system_admin());
REVOKE ALL ON integration.hmi_presence FROM PUBLIC;
GRANT SELECT ON integration.hmi_presence TO __RP1_APP_USER__;

CREATE FUNCTION integration.record_hmi_presence(
    p_connection_id uuid, p_installation_id uuid, p_session_id uuid,
    p_station_name text, p_bench_id text, p_sequence bigint,
    p_snapshot_age double precision, p_runs jsonb, p_plc jsonb
) RETURNS uuid
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, integration, iam
AS $$
DECLARE
    v_user_id bigint := iam.current_user_id();
    v_session_id bigint;
    v_result uuid;
BEGIN
    SELECT s.id INTO v_session_id
    FROM iam.auth_session s JOIN iam.app_user u ON u.id = s.user_id
    WHERE s.public_id = p_session_id AND s.user_id = v_user_id
      AND s.revoked_at IS NULL AND s.expires_at > clock_timestamp()
      AND u.enabled AND NOT u.must_change_password AND u.principal_kind = 'HUMAN'
      AND u.role IN ('SYSTEM_ADMIN', 'TEST_EXECUTOR');
    IF v_session_id IS NULL THEN
        RAISE EXCEPTION 'HMI requires an active operator session' USING ERRCODE = '42501';
    END IF;
    IF p_snapshot_age < 0 OR p_snapshot_age > 86400 OR p_snapshot_age = 'NaN'::double precision THEN
        RAISE EXCEPTION 'invalid snapshot age' USING ERRCODE = '22023';
    END IF;
    INSERT INTO integration.hmi_presence AS current(
        connection_id, installation_id, user_id, auth_session_id, station_name,
        bench_id, sequence, snapshot_at, runs, plc
    ) VALUES (
        p_connection_id, p_installation_id, v_user_id, v_session_id, p_station_name,
        p_bench_id, p_sequence, clock_timestamp() - make_interval(secs => p_snapshot_age), p_runs, p_plc
    ) ON CONFLICT (connection_id) DO UPDATE SET
        station_name = EXCLUDED.station_name, bench_id = EXCLUDED.bench_id,
        sequence = EXCLUDED.sequence, last_seen_at = clock_timestamp(),
        snapshot_at = EXCLUDED.snapshot_at, runs = EXCLUDED.runs, plc = EXCLUDED.plc
    WHERE current.user_id = v_user_id AND current.auth_session_id = v_session_id
      AND current.installation_id = p_installation_id AND current.disconnected_at IS NULL
      AND EXCLUDED.sequence > current.sequence
    RETURNING connection_id INTO v_result;
    IF v_result IS NULL THEN
        SELECT connection_id INTO v_result FROM integration.hmi_presence
        WHERE connection_id = p_connection_id AND user_id = v_user_id
          AND auth_session_id = v_session_id AND installation_id = p_installation_id
          AND disconnected_at IS NULL AND sequence >= p_sequence;
    END IF;
    RETURN v_result;
END;
$$;

CREATE FUNCTION integration.disconnect_hmi_presence(p_connection_id uuid, p_session_id uuid)
RETURNS boolean LANGUAGE sql SECURITY DEFINER
SET search_path = pg_catalog, integration, iam
AS $$
    WITH closed AS (
        UPDATE integration.hmi_presence p
        SET disconnected_at = coalesce(p.disconnected_at, clock_timestamp())
        FROM iam.auth_session s
        WHERE p.connection_id = p_connection_id AND p.user_id = iam.current_user_id()
          AND s.id = p.auth_session_id AND s.public_id = p_session_id
        RETURNING p.connection_id
    ) SELECT EXISTS(SELECT 1 FROM closed);
$$;

CREATE FUNCTION integration.list_hmi_presence(p_after uuid, p_limit integer)
RETURNS TABLE (
    connection_id uuid, installation_id uuid, station_name text, bench_id text,
    operator_name text, operator_display_name text, online boolean,
    connected_at timestamptz, last_seen_at timestamptz, snapshot_at timestamptz,
    runs jsonb, plc jsonb
) LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, integration, iam
AS $$
    WITH latest AS (
        SELECT DISTINCT ON (p.installation_id) p.*
        FROM integration.hmi_presence p
        WHERE p_after IS NULL OR p.installation_id > p_after
        ORDER BY p.installation_id, p.connected_at DESC, p.connection_id DESC
    )
    SELECT p.connection_id, p.installation_id, p.station_name, p.bench_id,
           u.username, u.display_name,
           p.disconnected_at IS NULL
               AND p.last_seen_at > statement_timestamp() - interval '45 seconds'
               AND s.revoked_at IS NULL AND s.expires_at > statement_timestamp()
               AND u.enabled AND NOT u.must_change_password AS online,
           p.connected_at, p.last_seen_at, p.snapshot_at, p.runs, p.plc
    FROM latest p
    JOIN iam.app_user u ON u.id = p.user_id
    JOIN iam.auth_session s ON s.id = p.auth_session_id
    WHERE iam.current_principal_kind() = 'HUMAN'
      AND (p.user_id = iam.current_user_id() OR iam.is_system_admin())
    ORDER BY p.installation_id
    LIMIT greatest(1, least(p_limit, 201));
$$;

REVOKE ALL ON FUNCTION integration.record_hmi_presence(uuid, uuid, uuid, text, text, bigint, double precision, jsonb, jsonb) FROM PUBLIC;
REVOKE ALL ON FUNCTION integration.disconnect_hmi_presence(uuid, uuid) FROM PUBLIC;
REVOKE ALL ON FUNCTION integration.list_hmi_presence(uuid, integer) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION integration.record_hmi_presence(uuid, uuid, uuid, text, text, bigint, double precision, jsonb, jsonb) TO __RP1_APP_USER__;
GRANT EXECUTE ON FUNCTION integration.disconnect_hmi_presence(uuid, uuid) TO __RP1_APP_USER__;
GRANT EXECUTE ON FUNCTION integration.list_hmi_presence(uuid, integer) TO __RP1_APP_USER__;
