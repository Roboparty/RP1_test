-- One disposition rule, one live-running meaning, and open read access for
-- signed-in humans. Formal exposure still requires a confirmed assessment.

CREATE OR REPLACE FUNCTION reliability.interval_disposition(
    p_interval_id bigint,
    p_scope_id bigint
)
RETURNS text
LANGUAGE sql
STABLE
SET search_path = pg_catalog, reliability
AS $$
    SELECT CASE
        WHEN p_scope_id IS NULL THEN 'EXCLUDED'
        WHEN EXISTS (
            SELECT 1
            FROM reliability.exposure_assessment ea
            JOIN reliability.exposure_scope_assignment esa ON esa.assessment_id = ea.id
            WHERE ea.runtime_interval_id = p_interval_id
              AND esa.scope_id = p_scope_id
              AND ea.eligible
              AND ea.assignment_status = 'CONFIRMED'
              AND ea.quality_status IN ('VALID', 'PARTIAL')
              AND esa.inclusion_status = 'INCLUDED'
        ) THEN 'INCLUDED'
        WHEN EXISTS (
            SELECT 1
            FROM reliability.exposure_assessment ea
            JOIN reliability.exposure_scope_assignment esa
              ON esa.assessment_id = ea.id AND esa.scope_id = p_scope_id
            WHERE ea.runtime_interval_id = p_interval_id
              AND (
                  ea.assignment_status IN ('INVALID', 'REJECTED')
                  OR ea.quality_status IN ('INVALID', 'MISSING')
                  OR esa.inclusion_status = 'EXCLUDED'
              )
              AND ea.assignment_status IS DISTINCT FROM 'PENDING_CONFIG_REVIEW'
              AND esa.inclusion_status IS DISTINCT FROM 'PENDING'
        ) THEN 'EXCLUDED'
        ELSE 'PENDING'
    END
$$;

REVOKE ALL ON FUNCTION reliability.interval_disposition(bigint, bigint) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION reliability.interval_disposition(bigint, bigint) TO __RP1_APP_USER__;

CREATE OR REPLACE FUNCTION iam.can_access_campaign(
    p_campaign_id bigint,
    p_require_edit boolean DEFAULT false
)
RETURNS boolean
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, iam
AS $$
    SELECT CASE
        WHEN p_campaign_id IS NULL THEN false
        WHEN iam.is_internal_service() THEN true
        WHEN iam.is_system_admin() THEN true
        WHEN NOT p_require_edit
             AND iam.is_authenticated()
             AND iam.current_user_role() IN ('VIEWER', 'TEST_EXECUTOR', 'SYSTEM_ADMIN') THEN true
        WHEN p_require_edit AND iam.current_user_role() <> 'TEST_EXECUTOR' THEN false
        ELSE EXISTS (
            SELECT 1
            FROM iam.user_campaign_access uca
            WHERE uca.user_id = iam.current_user_id()
              AND uca.campaign_id = p_campaign_id
              AND (NOT p_require_edit OR uca.access_level = 'EDIT')
        )
    END
$$;

ALTER TABLE test.test_execution
    ADD COLUMN live_connection_id uuid,
    ADD COLUMN live_session_key text,
    ADD COLUMN reported_active_seconds numeric(20,3) NOT NULL DEFAULT 0
        CHECK (reported_active_seconds >= 0);

ALTER TABLE test.test_execution
    ADD CONSTRAINT ck_live_session_key
    CHECK (live_session_key IS NULL OR length(live_session_key) BETWEEN 1 AND 128);

CREATE UNIQUE INDEX uq_test_execution_live_run
    ON test.test_execution (live_connection_id, live_session_key)
    WHERE status = 'RUNNING' AND live_connection_id IS NOT NULL;

CREATE OR REPLACE FUNCTION test.is_live_running(p_execution_id bigint)
RETURNS boolean
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, test, integration
AS $$
    SELECT EXISTS (
        SELECT 1
        FROM test.test_execution e
        JOIN integration.hmi_presence p ON p.connection_id = e.live_connection_id
        WHERE e.id = p_execution_id
          AND e.status = 'RUNNING'
          AND p.disconnected_at IS NULL
          AND p.last_seen_at > clock_timestamp() - interval '45 seconds'
    )
$$;

REVOKE ALL ON FUNCTION test.is_live_running(bigint) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION test.is_live_running(bigint) TO __RP1_APP_USER__;

CREATE OR REPLACE FUNCTION test.expire_stale_live_executions()
RETURNS integer
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, test, integration
AS $$
DECLARE
    v_closed integer := 0;
    v_row test.test_execution%ROWTYPE;
BEGIN
    FOR v_row IN
        SELECT e.*
        FROM test.test_execution e
        WHERE e.status = 'RUNNING'
          AND NOT test.is_live_running(e.id)
        FOR UPDATE
    LOOP
        UPDATE test.test_execution
        SET status = 'COMPLETED',
            normalized_ended_at = coalesce(normalized_ended_at, clock_timestamp())
        WHERE id = v_row.id
        RETURNING * INTO v_row;
        IF v_row.normalized_started_at IS NOT NULL
           AND NOT EXISTS (
               SELECT 1 FROM test.runtime_interval ri
               WHERE ri.execution_id = v_row.id AND NOT ri.voided
           )
        THEN
            INSERT INTO test.runtime_interval(
                campaign_id, asset_id, cycle_id, execution_id, source_kind,
                started_at, ended_at, active_seconds, clock_quality, data_quality, source_method
            ) VALUES (
                v_row.campaign_id, v_row.asset_id, v_row.cycle_id, v_row.id, 'NATIVE',
                v_row.normalized_started_at,
                v_row.normalized_started_at + make_interval(secs => v_row.reported_active_seconds),
                v_row.reported_active_seconds, 'VALID', 'PARTIAL', 'hmi-live-heartbeat'
            );
        END IF;
        v_closed := v_closed + 1;
    END LOOP;
    RETURN v_closed;
END
$$;

REVOKE ALL ON FUNCTION test.expire_stale_live_executions() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION test.expire_stale_live_executions() TO __RP1_APP_USER__;

CREATE OR REPLACE FUNCTION test.sync_live_runs(p_connection_id uuid)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, test, integration, iam, catalog
AS $$
DECLARE
    v_presence integration.hmi_presence%ROWTYPE;
    v_online boolean := false;
    v_run jsonb;
    v_key text;
    v_state text;
    v_active numeric(20,3);
    v_campaign bigint;
    v_cycle bigint;
    v_asset bigint;
    v_configuration bigint;
    v_version bigint;
    v_station bigint;
    v_execution test.test_execution%ROWTYPE;
    v_status text;
    v_result jsonb := '[]'::jsonb;
BEGIN
    PERFORM test.expire_stale_live_executions();
    SELECT * INTO v_presence FROM integration.hmi_presence WHERE connection_id = p_connection_id;
    IF NOT FOUND THEN
        RETURN v_result;
    END IF;
    v_online := v_presence.disconnected_at IS NULL
        AND v_presence.last_seen_at > clock_timestamp() - interval '45 seconds';
    IF NOT v_online THEN
        RETURN v_result;
    END IF;

    FOR v_run IN SELECT value FROM jsonb_array_elements(v_presence.runs)
    LOOP
        v_key := left(coalesce(v_run->>'session_key', ''), 128);
        v_state := lower(coalesce(v_run->>'state', ''));
        v_active := least(315360000, greatest(0, coalesce((v_run->>'active_seconds')::numeric, 0)));
        IF v_key = '' THEN
            CONTINUE;
        END IF;

        SELECT * INTO v_execution
        FROM test.test_execution
        WHERE live_connection_id = p_connection_id AND live_session_key = v_key
        ORDER BY id DESC
        LIMIT 1;

        v_campaign := NULL;
        IF v_state = 'running'
           AND coalesce(v_run->>'campaign_id', '') ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
           AND coalesce(v_run->>'cycle_id', '') ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
           AND coalesce(v_run->>'asset_id', '') ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
           AND coalesce(v_run->>'configuration_id', '') ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
           AND coalesce(v_run->>'test_case_version_id', '') ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
           AND coalesce(v_run->>'station_id', '') ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
        THEN
            SELECT c.id, cy.id, a.id, cfg.id, ver.id, s.id
            INTO v_campaign, v_cycle, v_asset, v_configuration, v_version, v_station
            FROM test.test_campaign c
            JOIN test.test_cycle cy ON cy.public_id = (v_run->>'cycle_id')::uuid AND cy.campaign_id = c.id
            JOIN test.asset a ON a.public_id = (v_run->>'asset_id')::uuid
            JOIN test.configuration_snapshot cfg ON cfg.public_id = (v_run->>'configuration_id')::uuid AND cfg.asset_id = a.id
            JOIN catalog.test_case_version ver ON ver.public_id = (v_run->>'test_case_version_id')::uuid
            JOIN test.station s ON s.public_id = (v_run->>'station_id')::uuid
            WHERE c.public_id = (v_run->>'campaign_id')::uuid;
        END IF;

        IF v_state = 'running' AND v_campaign IS NOT NULL THEN
            IF v_execution.id IS NOT NULL AND v_execution.status IN ('RUNNING', 'PAUSED') THEN
                UPDATE test.test_execution
                SET status = 'RUNNING',
                    reported_active_seconds = v_active,
                    received_at = clock_timestamp(),
                    operator_id = coalesce(operator_id, v_presence.user_id)
                WHERE id = v_execution.id
                RETURNING * INTO v_execution;
            ELSE
                INSERT INTO test.test_execution(
                    execution_code, campaign_id, cycle_id, asset_id, configuration_snapshot_id,
                    test_case_version_id, station_id, operator_id, status,
                    normalized_started_at, code_timestamp, source_time, received_at,
                    clock_quality, data_quality, live_connection_id, live_session_key,
                    reported_active_seconds
                ) VALUES (
                    'GENERATED', v_campaign, v_cycle, v_asset, v_configuration,
                    v_version, v_station, v_presence.user_id, 'RUNNING',
                    clock_timestamp(), clock_timestamp(), clock_timestamp(), clock_timestamp(),
                    'VALID', 'PARTIAL', p_connection_id, v_key, v_active
                )
                RETURNING * INTO v_execution;
            END IF;
        ELSIF v_execution.id IS NOT NULL AND v_execution.status = 'RUNNING' THEN
            v_status := CASE v_state
                WHEN 'paused' THEN 'PAUSED'
                WHEN 'fault' THEN 'FAILED'
                ELSE 'COMPLETED'
            END;
            UPDATE test.test_execution
            SET status = v_status,
                reported_active_seconds = v_active,
                received_at = clock_timestamp(),
                normalized_ended_at = CASE WHEN v_status = 'PAUSED' THEN NULL ELSE clock_timestamp() END
            WHERE id = v_execution.id
            RETURNING * INTO v_execution;
            IF v_status IN ('COMPLETED', 'FAILED')
               AND v_execution.normalized_started_at IS NOT NULL
               AND NOT EXISTS (
                   SELECT 1 FROM test.runtime_interval ri
                   WHERE ri.execution_id = v_execution.id AND NOT ri.voided
               )
            THEN
                INSERT INTO test.runtime_interval(
                    campaign_id, asset_id, cycle_id, execution_id, source_kind,
                    started_at, ended_at, active_seconds, clock_quality, data_quality, source_method
                ) VALUES (
                    v_execution.campaign_id, v_execution.asset_id, v_execution.cycle_id, v_execution.id,
                    'NATIVE', v_execution.normalized_started_at,
                    v_execution.normalized_started_at + make_interval(secs => v_execution.reported_active_seconds),
                    v_execution.reported_active_seconds, 'VALID', 'PARTIAL', 'hmi-live-heartbeat'
                );
            END IF;
        END IF;

        IF v_execution.id IS NOT NULL AND v_execution.live_session_key = v_key THEN
            v_result := v_result || jsonb_build_array(jsonb_build_object(
                'session_key', v_key,
                'execution_code', v_execution.execution_code,
                'execution_id', v_execution.public_id,
                'status', v_execution.status
            ));
        END IF;
        v_execution := NULL;
        v_campaign := NULL;
    END LOOP;
    RETURN v_result;
END
$$;

REVOKE ALL ON FUNCTION test.sync_live_runs(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION test.sync_live_runs(uuid) TO __RP1_APP_USER__;

-- Direct collector writes stay closed. Live runs and approved submissions use
-- the security-definer functions above and in the approval migration.
REVOKE INSERT, UPDATE, DELETE ON test.test_execution, test.runtime_interval, test.execution_result
    FROM __RP1_APP_USER__;

DO $$
DECLARE
    v_actor uuid;
BEGIN
    SELECT public_id INTO v_actor
    FROM iam.app_user
    WHERE role = 'SYSTEM_ADMIN' AND enabled AND principal_kind = 'HUMAN'
    ORDER BY id
    LIMIT 1;
    IF v_actor IS NOT NULL THEN
        PERFORM iam.set_request_context(v_actor, 'live-record-correction-20261013', '纠正无心跳的 RUNNING 与不合规则的执行单号');
    END IF;

    UPDATE test.test_execution AS e
    SET status = CASE
            WHEN r.source_status = 'failed' OR r.outcome = 'FAILED' THEN 'FAILED'
            WHEN r.source_status = 'blocked' THEN 'BLOCKED'
            ELSE 'COMPLETED'
        END
    FROM test.execution_result AS r
    WHERE r.execution_id = e.id
      AND e.status = 'RUNNING'
      AND e.live_connection_id IS NULL;

    UPDATE test.test_execution
    SET status = 'COMPLETED'
    WHERE status = 'RUNNING' AND live_connection_id IS NULL;

    UPDATE test.test_execution AS e
    SET code_timestamp = e.code_timestamp
    WHERE e.execution_code !~ '^[A-Z0-9]+(-[A-Z0-9]+)*_[0-9]{10}_RP[0-9]+\.[0-9]+-(SARM|SLEG|SYS|UPPER|LOWER|CHEST|HEAD|BAT)-[0-9]{3}_[A-Z0-9]+(-[A-Z0-9]+)*(_E[0-9]+)?$';
END
$$;
