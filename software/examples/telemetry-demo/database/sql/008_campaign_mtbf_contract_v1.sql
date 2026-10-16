-- Correct the superseded fixed-sample plan implementation and implement the
-- current Campaign/test-case-driven MTBF contract.

-- Remove objects that belong to the superseded Plan/Gate/dual-approval model.
DROP FUNCTION IF EXISTS reliability.enqueue_evaluation_recompute(bigint, bigint, text) CASCADE;
DROP FUNCTION IF EXISTS iam.has_plan_capability(bigint, text) CASCADE;
DROP FUNCTION IF EXISTS iam.can_access_mtbf_plan(bigint, boolean) CASCADE;
DROP FUNCTION IF EXISTS reliability.validate_human_approver() CASCADE;
DROP FUNCTION IF EXISTS reliability.reject_append_only_mutation() CASCADE;
DROP VIEW IF EXISTS reliability.current_exposure_disposition;
DROP VIEW IF EXISTS reliability.current_failure_adjudication;
DROP VIEW IF EXISTS reliability.current_plan_hold;

DROP TABLE IF EXISTS reliability.calculation_input_ref CASCADE;
DROP TABLE IF EXISTS reliability.calculation_member_result CASCADE;
DROP TABLE IF EXISTS reliability.plan_capability_assignment CASCADE;
DROP TABLE IF EXISTS reliability.plan_hold_revision CASCADE;
DROP TABLE IF EXISTS reliability.plan_state_transition CASCADE;
DROP TABLE IF EXISTS reliability.formal_conclusion_revision CASCADE;
DROP TABLE IF EXISTS reliability.plan_gate_evaluation_revision CASCADE;
DROP TABLE IF EXISTS reliability.exposure_disposition_revision CASCADE;
DROP TABLE IF EXISTS reliability.exposure_slice CASCADE;
DROP TABLE IF EXISTS reliability.failure_adjudication_revision CASCADE;
DROP TABLE IF EXISTS reliability.failure_case_relation CASCADE;
DROP TABLE IF EXISTS reliability.failure_case_event CASCADE;
DROP TABLE IF EXISTS reliability.failure_case CASCADE;
DROP TABLE IF EXISTS reliability.plan_control_revision CASCADE;
DROP TABLE IF EXISTS reliability.mtbf_plan_member CASCADE;
DROP TABLE IF EXISTS reliability.member_selection_revision CASCADE;
DROP TABLE IF EXISTS reliability.mtbf_plan_campaign CASCADE;
DROP TABLE IF EXISTS reliability.mtbf_plan_evaluation CASCADE;
DROP TABLE IF EXISTS reliability.mtbf_test_plan CASCADE;

ALTER TABLE reliability.mtbf_population
    DROP COLUMN IF EXISTS rule_hash,
    DROP COLUMN IF EXISTS snapshot_hash,
    DROP COLUMN IF EXISTS frozen_at,
    DROP COLUMN IF EXISTS frozen_by,
    DROP COLUMN IF EXISTS version;

-- The only new business table required by the current contract.
CREATE OR REPLACE FUNCTION reliability.valid_confidence_levels(p_levels numeric[])
RETURNS boolean
LANGUAGE sql
IMMUTABLE
AS $$
    SELECT cardinality(p_levels) > 0
       AND coalesce((SELECT bool_and(value > 0 AND value < 1) FROM unnest(p_levels) value), false)
$$;

CREATE TABLE reliability.campaign_mtbf_config (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    public_id uuid NOT NULL DEFAULT public.uuid_v7() UNIQUE,
    campaign_id bigint NOT NULL REFERENCES test.test_campaign(id) ON DELETE CASCADE,
    scope_id bigint NOT NULL REFERENCES reliability.mtbf_scope(id),
    method_id bigint NOT NULL REFERENCES reliability.statistics_method(id),
    target_seconds numeric(24,3) CHECK (target_seconds IS NULL OR target_seconds > 0),
    confidence_levels numeric(6,5)[] NOT NULL DEFAULT ARRAY[0.70, 0.90]::numeric(6,5)[],
    enabled boolean NOT NULL DEFAULT true,
    settings jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_by bigint REFERENCES iam.app_user(id),
    updated_by bigint REFERENCES iam.app_user(id),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (campaign_id, scope_id),
    CHECK (reliability.valid_confidence_levels(confidence_levels))
);

ALTER TABLE reliability.interruption
    ADD COLUMN IF NOT EXISTS failure_group_key text;

CREATE INDEX IF NOT EXISTS idx_interruption_failure_group
    ON reliability.interruption(campaign_id, failure_group_key)
    WHERE NOT voided AND classification = 'FAILED';

-- Calculation snapshots are now owned directly by a Campaign/scope.
ALTER TABLE reliability.calculation_run
    ADD COLUMN IF NOT EXISTS campaign_id bigint REFERENCES test.test_campaign(id),
    ADD COLUMN IF NOT EXISTS knowledge_as_of_at timestamptz NOT NULL DEFAULT now(),
    ADD COLUMN IF NOT EXISTS implementation_version text NOT NULL DEFAULT '';

ALTER TABLE reliability.calculation_run
    ALTER COLUMN population_id DROP NOT NULL,
    DROP COLUMN IF EXISTS plan_id,
    DROP COLUMN IF EXISTS evaluation_id,
    DROP COLUMN IF EXISTS dependency_hash,
    DROP COLUMN IF EXISTS formal_exposure_seconds,
    DROP COLUMN IF EXISTS provisional_exposure_seconds,
    DROP COLUMN IF EXISTS pending_exposure_seconds,
    DROP COLUMN IF EXISTS excluded_exposure_seconds,
    DROP COLUMN IF EXISTS relevant_failure_count,
    DROP COLUMN IF EXISTS pending_failure_case_count,
    DROP COLUMN IF EXISTS point_estimate_hours,
    DROP COLUMN IF EXISTS lower_70_hours,
    DROP COLUMN IF EXISTS lower_90_hours,
    DROP COLUMN IF EXISTS point_estimate_status;

ALTER TABLE reliability.calculation_run
    DROP CONSTRAINT IF EXISTS calculation_run_status_check;
ALTER TABLE reliability.calculation_run
    ADD CONSTRAINT calculation_run_status_check
    CHECK (status IN ('QUEUED', 'RUNNING', 'SUCCEEDED', 'FAILED'));

CREATE INDEX IF NOT EXISTS idx_calculation_campaign_scope
    ON reliability.calculation_run(campaign_id, scope_id, created_at DESC);

ALTER TABLE reliability.current_mtbf_result
    ADD COLUMN IF NOT EXISTS campaign_id bigint REFERENCES test.test_campaign(id),
    ADD COLUMN IF NOT EXISTS excluded_exposure_seconds numeric(24,3) NOT NULL DEFAULT 0
        CHECK (excluded_exposure_seconds >= 0),
    ADD COLUMN IF NOT EXISTS lower_70_hours numeric(24,9),
    ADD COLUMN IF NOT EXISTS lower_90_hours numeric(24,9);

ALTER TABLE reliability.current_mtbf_result
    ALTER COLUMN population_id DROP NOT NULL,
    DROP COLUMN IF EXISTS plan_id,
    DROP COLUMN IF EXISTS evaluation_id,
    DROP COLUMN IF EXISTS freshness,
    DROP COLUMN IF EXISTS stale_reason,
    DROP COLUMN IF EXISTS pending_job_count,
    DROP COLUMN IF EXISTS provisional_exposure_seconds;

DROP INDEX IF EXISTS reliability.uq_current_mtbf_result_evaluation;
ALTER TABLE reliability.current_mtbf_result
    DROP CONSTRAINT IF EXISTS current_mtbf_result_population_id_scope_id_key;
CREATE UNIQUE INDEX uq_current_mtbf_result_campaign_scope
    ON reliability.current_mtbf_result(campaign_id, scope_id)
    WHERE campaign_id IS NOT NULL;

-- Formal reports remain optional and population based; remove only the old
-- plan-revision projection fields.
ALTER TABLE reliability.current_conclusion
    DROP COLUMN IF EXISTS plan_id,
    DROP COLUMN IF EXISTS evaluation_id,
    DROP COLUMN IF EXISTS conclusion_revision_id,
    DROP COLUMN IF EXISTS publication_validity,
    DROP COLUMN IF EXISTS freshness,
    DROP COLUMN IF EXISTS stale_reason;
DROP INDEX IF EXISTS reliability.uq_current_conclusion_evaluation;
ALTER TABLE reliability.current_conclusion
    DROP CONSTRAINT IF EXISTS current_conclusion_population_id_scope_id_key;
ALTER TABLE reliability.current_conclusion
    ADD CONSTRAINT current_conclusion_population_id_scope_id_key
    UNIQUE (population_id, scope_id);

ALTER TABLE reliability.recompute_job
    ADD COLUMN IF NOT EXISTS scope_id bigint REFERENCES reliability.mtbf_scope(id),
    ADD COLUMN IF NOT EXISTS job_key text NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS max_attempts integer NOT NULL DEFAULT 5 CHECK (max_attempts > 0),
    ADD COLUMN IF NOT EXISTS priority integer NOT NULL DEFAULT 100;

ALTER TABLE reliability.recompute_job
    DROP COLUMN IF EXISTS plan_id,
    DROP COLUMN IF EXISTS evaluation_id,
    DROP COLUMN IF EXISTS job_kind,
    DROP COLUMN IF EXISTS dependency_hash;

DROP INDEX IF EXISTS reliability.uq_recompute_active_job_key;
CREATE UNIQUE INDEX uq_recompute_active_job_key
    ON reliability.recompute_job(job_key)
    WHERE job_key <> '' AND status IN ('PENDING', 'PROCESSING');
CREATE INDEX IF NOT EXISTS idx_recompute_campaign_scope
    ON reliability.recompute_job(campaign_id, scope_id, requested_at DESC);

-- Publish the current method without rewriting the original placeholder.
INSERT INTO reliability.statistics_method (
    method_code, version, name, asset_kind, parameter_schema,
    default_parameters, implementation_key, status
) VALUES (
    'POISSON_EXPOSURE_ESTIMATE', '1.0', '泊松暴露量 MTBF 实时估计', NULL,
    '{"type":"object","properties":{"confidence_levels":{"type":"array","items":{"type":"number","exclusiveMinimum":0,"exclusiveMaximum":1}}}}',
    '{"confidence_levels":[0.70,0.90]}',
    'mtbf.poisson.exposure_estimate.v1', 'PUBLISHED'
) ON CONFLICT (method_code, version) DO NOTHING;

UPDATE reliability.statistics_method
SET status = 'RETIRED', updated_at = clock_timestamp()
WHERE implementation_key = 'mtbf.poisson.fixed_time_censored.v1'
  AND status = 'PUBLISHED';

-- Internal workers need campaign-scoped reads while human users continue to
-- be constrained by explicit Campaign grants and global role.
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

-- Direct campaign access is authoritative for calculation snapshots.
CREATE OR REPLACE FUNCTION iam.can_access_calculation(
    p_calculation_run_id bigint,
    p_require_edit boolean DEFAULT false
)
RETURNS boolean
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, iam, reliability
AS $$
    SELECT iam.is_internal_service() OR iam.is_system_admin() OR EXISTS (
        SELECT 1
        FROM reliability.calculation_run r
        WHERE r.id = p_calculation_run_id
          AND (
              (r.campaign_id IS NOT NULL AND iam.can_access_campaign(r.campaign_id, p_require_edit))
              OR (
                  r.campaign_id IS NULL
                  AND EXISTS (
                      SELECT 1
                      FROM reliability.calculation_contributor_campaign ccc
                      WHERE ccc.calculation_run_id = r.id
                        AND iam.can_access_campaign(ccc.campaign_id, p_require_edit)
                  )
              )
          )
    )
$$;

-- Enqueue one coalesced Campaign/scope task. Repeating an API idempotency key
-- returns the existing task even after it has completed.
CREATE OR REPLACE FUNCTION reliability.enqueue_campaign_recompute(
    p_campaign_id bigint,
    p_scope_id bigint,
    p_reason text,
    p_idempotency_key text DEFAULT NULL
)
RETURNS reliability.recompute_job
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, reliability, iam
AS $$
DECLARE
    v_job reliability.recompute_job;
    v_key text := 'campaign:' || p_campaign_id::text || ':scope:' || p_scope_id::text;
BEGIN
    IF NOT (iam.is_internal_service() OR iam.can_access_campaign(p_campaign_id, true)) THEN
        RAISE EXCEPTION 'campaign write access denied' USING ERRCODE = '42501';
    END IF;

    IF p_idempotency_key IS NOT NULL AND btrim(p_idempotency_key) <> '' THEN
        PERFORM pg_advisory_xact_lock(hashtextextended(
            p_campaign_id::text || ':' || p_scope_id::text || ':' || p_idempotency_key, 0
        ));
        SELECT * INTO v_job
        FROM reliability.recompute_job
        WHERE campaign_id = p_campaign_id
          AND scope_id = p_scope_id
          AND (
              payload ->> 'idempotency_key' = p_idempotency_key
              OR coalesce(payload -> 'idempotency_keys', '[]'::jsonb) ? p_idempotency_key
          )
        ORDER BY id DESC
        LIMIT 1;
        IF v_job.id IS NOT NULL THEN
            RETURN v_job;
        END IF;
    END IF;

    INSERT INTO reliability.recompute_job (
        campaign_id, scope_id, job_key, resource_kind, resource_key,
        reason, payload, status, available_at
    ) VALUES (
        p_campaign_id, p_scope_id, v_key,
        'test.test_campaign', p_campaign_id::text,
        p_reason,
        jsonb_strip_nulls(jsonb_build_object(
            'campaign_id', p_campaign_id,
            'scope_id', p_scope_id,
            'idempotency_key', nullif(btrim(p_idempotency_key), '')
        )),
        'PENDING', clock_timestamp()
    )
    ON CONFLICT (job_key) WHERE job_key <> '' AND status IN ('PENDING', 'PROCESSING')
    DO UPDATE SET
        reason = EXCLUDED.reason,
        requested_at = clock_timestamp(),
        updated_at = clock_timestamp(),
        payload = CASE
            WHEN EXCLUDED.payload ? 'idempotency_key' THEN
                reliability.recompute_job.payload || jsonb_build_object(
                    'idempotency_keys',
                    coalesce(reliability.recompute_job.payload -> 'idempotency_keys', '[]'::jsonb)
                    || jsonb_build_array(EXCLUDED.payload ->> 'idempotency_key')
                )
            ELSE reliability.recompute_job.payload
        END
    RETURNING * INTO v_job;

    RETURN v_job;
END;
$$;

-- Route watched data changes to every enabled scope of the affected Campaign.
CREATE OR REPLACE FUNCTION reliability.enqueue_recompute_from_change()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, reliability, test
AS $$
DECLARE
    v_data jsonb := coalesce(to_jsonb(NEW), to_jsonb(OLD));
    v_campaign_id bigint;
    v_scope_id bigint;
    v_config record;
    v_identity text;
BEGIN
    v_identity := coalesce(v_data ->> 'public_id', v_data ->> 'id', v_data ->> 'asset_id', 'unknown');

    IF TG_TABLE_SCHEMA = 'reliability' AND TG_TABLE_NAME = 'interruption_scope_assignment' THEN
        SELECT i.campaign_id, (v_data ->> 'scope_id')::bigint
        INTO v_campaign_id, v_scope_id
        FROM reliability.interruption i
        WHERE i.id = (v_data ->> 'interruption_id')::bigint;
    ELSIF TG_TABLE_SCHEMA = 'reliability' AND TG_TABLE_NAME = 'campaign_mtbf_config' THEN
        v_campaign_id := (v_data ->> 'campaign_id')::bigint;
        v_scope_id := (v_data ->> 'scope_id')::bigint;
    ELSIF TG_TABLE_SCHEMA = 'reliability' AND TG_TABLE_NAME = 'exposure_scope_assignment' THEN
        v_campaign_id := (v_data ->> 'campaign_id')::bigint;
        v_scope_id := (v_data ->> 'scope_id')::bigint;
    ELSE
        v_campaign_id := nullif(v_data ->> 'campaign_id', '')::bigint;
    END IF;

    IF v_campaign_id IS NULL AND v_data ? 'asset_id' THEN
        FOR v_config IN
            SELECT c.campaign_id, c.scope_id
            FROM test.campaign_asset ca
            JOIN reliability.campaign_mtbf_config c ON c.campaign_id = ca.campaign_id
            WHERE ca.asset_id = (v_data ->> 'asset_id')::bigint AND c.enabled
        LOOP
            PERFORM reliability.enqueue_campaign_recompute(
                v_config.campaign_id, v_config.scope_id,
                TG_TABLE_SCHEMA || '.' || TG_TABLE_NAME || ':' || TG_OP || ':' || v_identity,
                NULL
            );
        END LOOP;
        IF TG_OP = 'DELETE' THEN RETURN OLD; ELSE RETURN NEW; END IF;
    END IF;

    FOR v_config IN
        SELECT c.campaign_id, c.scope_id
        FROM reliability.campaign_mtbf_config c
        WHERE c.campaign_id = v_campaign_id
          AND c.enabled
          AND (v_scope_id IS NULL OR c.scope_id = v_scope_id)
    LOOP
        PERFORM reliability.enqueue_campaign_recompute(
            v_config.campaign_id, v_config.scope_id,
            TG_TABLE_SCHEMA || '.' || TG_TABLE_NAME || ':' || TG_OP || ':' || v_identity,
            NULL
        );
    END LOOP;
    IF TG_OP = 'DELETE' THEN RETURN OLD; ELSE RETURN NEW; END IF;
END;
$$;

DROP TRIGGER IF EXISTS trg_enqueue_recompute ON reliability.campaign_mtbf_config;
CREATE TRIGGER trg_enqueue_recompute
AFTER INSERT OR UPDATE ON reliability.campaign_mtbf_config
FOR EACH ROW EXECUTE FUNCTION reliability.enqueue_recompute_from_change();

-- Claim the oldest ready job with SKIP LOCKED; priority is operational only.
CREATE OR REPLACE FUNCTION reliability.claim_recompute_job(p_worker_id text)
RETURNS SETOF reliability.recompute_job
LANGUAGE sql
SECURITY DEFINER
SET search_path = pg_catalog, reliability
AS $$
    UPDATE reliability.recompute_job
    SET status = 'PROCESSING', attempts = attempts + 1,
        locked_by = p_worker_id, locked_at = clock_timestamp(),
        updated_at = clock_timestamp()
    WHERE id = (
        SELECT id FROM reliability.recompute_job
        WHERE status = 'PENDING' AND available_at <= clock_timestamp()
        ORDER BY priority, requested_at, id
        FOR UPDATE SKIP LOCKED
        LIMIT 1
    )
    RETURNING *
$$;

-- RLS stays campaign-scoped; the internal worker may calculate but cannot be
-- mistaken for a human approver.
ALTER TABLE reliability.campaign_mtbf_config ENABLE ROW LEVEL SECURITY;
CREATE POLICY campaign_mtbf_config_select ON reliability.campaign_mtbf_config
    FOR SELECT USING (iam.can_access_campaign(campaign_id, false));
CREATE POLICY campaign_mtbf_config_write ON reliability.campaign_mtbf_config
    FOR ALL USING (iam.can_access_campaign(campaign_id, true))
    WITH CHECK (iam.can_access_campaign(campaign_id, true));

DROP POLICY IF EXISTS calculation_select ON reliability.calculation_run;
DROP POLICY IF EXISTS calculation_admin_write ON reliability.calculation_run;
DROP POLICY IF EXISTS calculation_write ON reliability.calculation_run;
CREATE POLICY calculation_select ON reliability.calculation_run
    FOR SELECT USING (iam.can_access_calculation(id, false));
CREATE POLICY calculation_write ON reliability.calculation_run
    FOR ALL USING (
        iam.is_internal_service() OR iam.is_system_admin()
        OR (campaign_id IS NOT NULL AND iam.can_access_campaign(campaign_id, true))
    ) WITH CHECK (
        iam.is_internal_service() OR iam.is_system_admin()
        OR (campaign_id IS NOT NULL AND iam.can_access_campaign(campaign_id, true))
    );

DROP POLICY IF EXISTS calculation_campaign_admin_write ON reliability.calculation_contributor_campaign;
CREATE POLICY calculation_campaign_write ON reliability.calculation_contributor_campaign
    FOR ALL USING (iam.is_internal_service() OR iam.can_access_campaign(campaign_id, true))
    WITH CHECK (iam.is_internal_service() OR iam.can_access_campaign(campaign_id, true));

DROP POLICY IF EXISTS calculation_result_admin_write ON reliability.current_mtbf_result;
CREATE POLICY calculation_result_write ON reliability.current_mtbf_result
    FOR ALL USING (iam.is_internal_service() OR iam.is_system_admin())
    WITH CHECK (iam.is_internal_service() OR iam.is_system_admin());

DROP POLICY IF EXISTS recompute_admin ON reliability.recompute_job;
DROP POLICY IF EXISTS recompute_worker ON reliability.recompute_job;
CREATE POLICY recompute_worker ON reliability.recompute_job
    FOR ALL USING (iam.is_internal_service() OR iam.is_system_admin())
    WITH CHECK (iam.is_internal_service() OR iam.is_system_admin());

DROP TRIGGER IF EXISTS trg_audit_row_change ON reliability.campaign_mtbf_config;
CREATE TRIGGER trg_audit_row_change
AFTER INSERT OR UPDATE OR DELETE ON reliability.campaign_mtbf_config
FOR EACH ROW EXECUTE FUNCTION audit.capture_row_change();

GRANT SELECT, INSERT, UPDATE ON reliability.campaign_mtbf_config TO __RP1_APP_USER__;
GRANT SELECT ON reliability.campaign_mtbf_config TO __RP1_READONLY_USER__;
GRANT USAGE, SELECT ON SEQUENCE reliability.campaign_mtbf_config_id_seq TO __RP1_APP_USER__;
GRANT EXECUTE ON FUNCTION reliability.enqueue_campaign_recompute(bigint, bigint, text, text)
TO __RP1_APP_USER__;
GRANT EXECUTE ON FUNCTION reliability.claim_recompute_job(text) TO __RP1_APP_USER__;
