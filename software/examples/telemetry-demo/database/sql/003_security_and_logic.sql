-- ---------------------------------------------------------------------------
-- Request context and authorization helpers
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION iam.current_user_id()
RETURNS bigint
LANGUAGE sql
STABLE
AS $$
    SELECT nullif(current_setting('app.user_id', true), '')::bigint
$$;

CREATE OR REPLACE FUNCTION iam.current_user_role()
RETURNS text
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, iam
AS $$
    SELECT role
    FROM iam.app_user
    WHERE id = iam.current_user_id()
      AND enabled
$$;

CREATE OR REPLACE FUNCTION iam.is_authenticated()
RETURNS boolean
LANGUAGE sql
STABLE
AS $$
    SELECT iam.current_user_role() IS NOT NULL
$$;

CREATE OR REPLACE FUNCTION iam.is_system_admin()
RETURNS boolean
LANGUAGE sql
STABLE
AS $$
    SELECT coalesce(iam.current_user_role() = 'SYSTEM_ADMIN', false)
$$;

CREATE OR REPLACE FUNCTION iam.can_write_business()
RETURNS boolean
LANGUAGE sql
STABLE
AS $$
    SELECT coalesce(iam.current_user_role() IN ('TEST_EXECUTOR', 'SYSTEM_ADMIN'), false)
$$;

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

CREATE OR REPLACE FUNCTION iam.can_access_asset(
    p_asset_id bigint,
    p_require_edit boolean DEFAULT false
)
RETURNS boolean
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, iam, test
AS $$
    SELECT iam.is_system_admin() OR EXISTS (
        SELECT 1
        FROM test.campaign_asset ca
        WHERE ca.asset_id = p_asset_id
          AND iam.can_access_campaign(ca.campaign_id, p_require_edit)
    )
$$;

CREATE OR REPLACE FUNCTION iam.can_access_population(
    p_population_id bigint,
    p_require_edit boolean DEFAULT false
)
RETURNS boolean
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, iam, reliability
AS $$
    SELECT iam.is_system_admin() OR EXISTS (
        SELECT 1
        FROM reliability.population_campaign pc
        WHERE pc.population_id = p_population_id
          AND iam.can_access_campaign(pc.campaign_id, p_require_edit)
    )
$$;

CREATE OR REPLACE FUNCTION iam.can_access_cohort(
    p_cohort_id bigint,
    p_require_edit boolean DEFAULT false
)
RETURNS boolean
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, iam, health
AS $$
    SELECT iam.is_system_admin() OR EXISTS (
        SELECT 1
        FROM health.reference_cohort_campaign hcc
        WHERE hcc.cohort_id = p_cohort_id
          AND iam.can_access_campaign(hcc.campaign_id, p_require_edit)
    )
$$;

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
    SELECT iam.is_system_admin() OR (
        EXISTS (
            SELECT 1
            FROM reliability.calculation_contributor_campaign ccc
            WHERE ccc.calculation_run_id = p_calculation_run_id
        )
        AND NOT EXISTS (
            SELECT 1
            FROM reliability.calculation_contributor_campaign ccc
            WHERE ccc.calculation_run_id = p_calculation_run_id
              AND NOT iam.can_access_campaign(ccc.campaign_id, p_require_edit)
        )
    )
$$;

CREATE OR REPLACE FUNCTION iam.set_request_context(
    p_user_public_id uuid,
    p_request_id text,
    p_change_reason text DEFAULT ''
)
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, iam
AS $$
DECLARE
    v_user_id bigint;
BEGIN
    SELECT id INTO v_user_id
    FROM iam.app_user
    WHERE public_id = p_user_public_id
      AND enabled;

    IF v_user_id IS NULL THEN
        RAISE EXCEPTION 'unknown or disabled application user';
    END IF;

    PERFORM set_config('app.user_id', v_user_id::text, true);
    PERFORM set_config('app.request_id', coalesce(p_request_id, ''), true);
    PERFORM set_config('app.change_reason', coalesce(p_change_reason, ''), true);
END;
$$;

-- ---------------------------------------------------------------------------
-- Cross-table domain invariants
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION test.validate_asset_batch_kind()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    v_kind text;
BEGIN
    IF NEW.batch_id IS NOT NULL THEN
        SELECT asset_kind INTO v_kind FROM test.manufacturing_batch WHERE id = NEW.batch_id;
        IF v_kind IS DISTINCT FROM NEW.asset_kind THEN
            RAISE EXCEPTION 'asset kind % does not match manufacturing batch kind %', NEW.asset_kind, v_kind;
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_asset_batch_kind
BEFORE INSERT OR UPDATE OF batch_id, asset_kind ON test.asset
FOR EACH ROW EXECUTE FUNCTION test.validate_asset_batch_kind();

CREATE OR REPLACE FUNCTION test.validate_asset_profile_kind()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    v_kind text;
    v_expected text := TG_ARGV[0];
BEGIN
    SELECT asset_kind INTO v_kind FROM test.asset WHERE id = NEW.asset_id;
    IF v_kind IS DISTINCT FROM v_expected THEN
        RAISE EXCEPTION 'asset % has kind %, expected % profile', NEW.asset_id, v_kind, v_expected;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_whole_machine_profile_kind
BEFORE INSERT OR UPDATE OF asset_id ON test.whole_machine_profile
FOR EACH ROW EXECUTE FUNCTION test.validate_asset_profile_kind('WHOLE_MACHINE');

CREATE TRIGGER trg_module_profile_kind
BEFORE INSERT OR UPDATE OF asset_id ON test.module_profile
FOR EACH ROW EXECUTE FUNCTION test.validate_asset_profile_kind('MODULE');

CREATE OR REPLACE FUNCTION test.validate_campaign_asset_kind()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    v_campaign_kind text;
    v_asset_kind text;
BEGIN
    SELECT asset_kind INTO v_campaign_kind FROM test.test_campaign WHERE id = NEW.campaign_id;
    SELECT asset_kind INTO v_asset_kind FROM test.asset WHERE id = NEW.asset_id;
    IF v_campaign_kind IS DISTINCT FROM v_asset_kind THEN
        RAISE EXCEPTION 'campaign kind % cannot contain asset kind %', v_campaign_kind, v_asset_kind;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_campaign_asset_kind
BEFORE INSERT OR UPDATE ON test.campaign_asset
FOR EACH ROW EXECUTE FUNCTION test.validate_campaign_asset_kind();

CREATE OR REPLACE FUNCTION test.validate_cycle_links()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    v_campaign_kind text;
    v_asset_kind text;
BEGIN
    SELECT asset_kind INTO v_campaign_kind FROM test.test_campaign WHERE id = NEW.campaign_id;
    SELECT asset_kind INTO v_asset_kind FROM test.asset WHERE id = NEW.asset_id;
    IF v_campaign_kind IS DISTINCT FROM v_asset_kind THEN
        RAISE EXCEPTION 'cycle campaign kind % does not match asset kind %', v_campaign_kind, v_asset_kind;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM test.campaign_asset
        WHERE campaign_id = NEW.campaign_id AND asset_id = NEW.asset_id
    ) THEN
        RAISE EXCEPTION 'asset % is not assigned to campaign %', NEW.asset_id, NEW.campaign_id;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_cycle_links
BEFORE INSERT OR UPDATE OF campaign_id, asset_id ON test.test_cycle
FOR EACH ROW EXECUTE FUNCTION test.validate_cycle_links();

CREATE OR REPLACE FUNCTION test.validate_execution_links()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    v_cycle_campaign bigint;
    v_cycle_asset bigint;
    v_config_asset bigint;
    v_campaign_kind text;
    v_case_kind text;
BEGIN
    SELECT campaign_id, asset_id INTO v_cycle_campaign, v_cycle_asset
    FROM test.test_cycle WHERE id = NEW.cycle_id;
    SELECT asset_id INTO v_config_asset
    FROM test.configuration_snapshot WHERE id = NEW.configuration_snapshot_id;
    SELECT c.asset_kind INTO v_campaign_kind
    FROM test.test_campaign c WHERE c.id = NEW.campaign_id;
    SELECT tc.asset_kind INTO v_case_kind
    FROM catalog.test_case_version tcv
    JOIN catalog.test_case tc ON tc.id = tcv.test_case_id
    WHERE tcv.id = NEW.test_case_version_id;

    IF v_cycle_campaign IS DISTINCT FROM NEW.campaign_id OR v_cycle_asset IS DISTINCT FROM NEW.asset_id THEN
        RAISE EXCEPTION 'execution cycle, campaign and asset do not agree';
    END IF;
    IF v_config_asset IS DISTINCT FROM NEW.asset_id THEN
        RAISE EXCEPTION 'execution configuration does not belong to asset';
    END IF;
    IF v_campaign_kind IS DISTINCT FROM v_case_kind THEN
        RAISE EXCEPTION 'test case kind % does not match campaign kind %', v_case_kind, v_campaign_kind;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_execution_links
BEFORE INSERT OR UPDATE OF campaign_id, cycle_id, asset_id, configuration_snapshot_id, test_case_version_id
ON test.test_execution
FOR EACH ROW EXECUTE FUNCTION test.validate_execution_links();

CREATE OR REPLACE FUNCTION test.validate_stage_campaign()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    v_campaign bigint;
BEGIN
    SELECT campaign_id INTO v_campaign FROM test.test_execution WHERE id = NEW.execution_id;
    IF v_campaign IS DISTINCT FROM NEW.campaign_id THEN
        RAISE EXCEPTION 'stage campaign does not match execution campaign';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_stage_campaign
BEFORE INSERT OR UPDATE OF campaign_id, execution_id ON test.execution_stage
FOR EACH ROW EXECUTE FUNCTION test.validate_stage_campaign();

CREATE OR REPLACE FUNCTION test.validate_runtime_links()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    v_campaign bigint;
    v_asset bigint;
BEGIN
    IF NEW.execution_id IS NOT NULL THEN
        SELECT campaign_id, asset_id INTO v_campaign, v_asset
        FROM test.test_execution WHERE id = NEW.execution_id;
        IF v_campaign IS DISTINCT FROM NEW.campaign_id OR v_asset IS DISTINCT FROM NEW.asset_id THEN
            RAISE EXCEPTION 'runtime interval does not match execution campaign/asset';
        END IF;
    END IF;
    IF NEW.stage_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM test.execution_stage
        WHERE id = NEW.stage_id
          AND campaign_id = NEW.campaign_id
          AND (NEW.execution_id IS NULL OR execution_id = NEW.execution_id)
    ) THEN
        RAISE EXCEPTION 'runtime interval stage does not match execution/campaign';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_runtime_links
BEFORE INSERT OR UPDATE OF campaign_id, asset_id, execution_id, stage_id ON test.runtime_interval
FOR EACH ROW EXECUTE FUNCTION test.validate_runtime_links();

CREATE OR REPLACE FUNCTION reliability.validate_population_membership_kind()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    v_population_kind text;
    v_asset_kind text;
BEGIN
    SELECT asset_kind INTO v_population_kind
    FROM reliability.mtbf_population WHERE id = NEW.population_id;
    SELECT a.asset_kind INTO v_asset_kind
    FROM test.configuration_snapshot cs
    JOIN test.asset a ON a.id = cs.asset_id
    WHERE cs.id = NEW.configuration_snapshot_id;
    IF v_population_kind IS DISTINCT FROM v_asset_kind THEN
        RAISE EXCEPTION 'population kind % does not match configuration asset kind %', v_population_kind, v_asset_kind;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_population_membership_kind
BEFORE INSERT OR UPDATE OF population_id, configuration_snapshot_id
ON reliability.population_membership
FOR EACH ROW EXECUTE FUNCTION reliability.validate_population_membership_kind();

CREATE OR REPLACE FUNCTION reliability.validate_exposure_assessment()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    v_source_kind text;
    v_campaign bigint;
    v_asset bigint;
    v_config_asset bigint;
BEGIN
    SELECT source_kind, campaign_id, asset_id
    INTO v_source_kind, v_campaign, v_asset
    FROM test.runtime_interval
    WHERE id = NEW.runtime_interval_id;

    IF v_campaign IS DISTINCT FROM NEW.campaign_id OR v_asset IS DISTINCT FROM NEW.asset_id THEN
        RAISE EXCEPTION 'exposure assessment does not match runtime interval campaign/asset';
    END IF;
    IF NEW.eligible AND v_source_kind = 'LEGACY_REPORTED' THEN
        RAISE EXCEPTION 'legacy reported test time cannot be MTBF eligible';
    END IF;
    IF NEW.configuration_snapshot_id IS NOT NULL THEN
        SELECT asset_id INTO v_config_asset
        FROM test.configuration_snapshot WHERE id = NEW.configuration_snapshot_id;
        IF v_config_asset IS DISTINCT FROM NEW.asset_id THEN
            RAISE EXCEPTION 'exposure configuration does not belong to asset';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_exposure_assessment
BEFORE INSERT OR UPDATE ON reliability.exposure_assessment
FOR EACH ROW EXECUTE FUNCTION reliability.validate_exposure_assessment();

CREATE OR REPLACE FUNCTION reliability.validate_failed_error_code()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    v_catalog_status text;
BEGIN
    IF NEW.classification = 'FAILED' THEN
        SELECT ecv.status INTO v_catalog_status
        FROM catalog.error_code ec
        JOIN catalog.error_catalog_version ecv ON ecv.id = ec.catalog_version_id
        WHERE ec.id = NEW.primary_error_code_id
          AND ec.enabled;
        IF v_catalog_status IS DISTINCT FROM 'PUBLISHED' THEN
            RAISE EXCEPTION 'FAILED interruption requires an enabled error code from a published catalog';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_failed_error_code
BEFORE INSERT OR UPDATE OF classification, primary_error_code_id
ON reliability.interruption
FOR EACH ROW EXECUTE FUNCTION reliability.validate_failed_error_code();

CREATE OR REPLACE FUNCTION health.validate_reference_membership_kind()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    v_cohort_kind text;
    v_asset_kind text;
BEGIN
    SELECT asset_kind INTO v_cohort_kind FROM health.reference_cohort WHERE id = NEW.cohort_id;
    SELECT asset_kind INTO v_asset_kind FROM test.asset WHERE id = NEW.asset_id;
    IF v_cohort_kind IS DISTINCT FROM v_asset_kind THEN
        RAISE EXCEPTION 'reference cohort kind % does not match asset kind %', v_cohort_kind, v_asset_kind;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_reference_membership_kind
BEFORE INSERT OR UPDATE OF cohort_id, asset_id ON health.reference_membership
FOR EACH ROW EXECUTE FUNCTION health.validate_reference_membership_kind();

-- ---------------------------------------------------------------------------
-- Updated-at triggers
-- ---------------------------------------------------------------------------

DO $$
DECLARE
    v_table text;
BEGIN
    FOREACH v_table IN ARRAY ARRAY[
        'iam.app_user', 'iam.user_credential',
        'integration.ingestion_source',
        'test.site', 'test.lab', 'test.station', 'test.manufacturing_batch',
        'test.asset', 'test.whole_machine_profile', 'test.module_profile',
        'test.configuration_snapshot', 'test.test_program', 'test.test_campaign',
        'catalog.mission_tag', 'catalog.test_case', 'catalog.test_case_version',
        'catalog.configuration_equivalence_rule', 'test.configuration_change',
        'test.campaign_coverage_item', 'test.test_cycle', 'test.analysis_segment',
        'test.test_execution', 'test.execution_stage', 'test.runtime_interval',
        'test.test_event', 'catalog.error_catalog_version', 'catalog.error_code',
        'catalog.source_error_mapping', 'catalog.metric_definition',
        'catalog.metric_version', 'catalog.health_check_policy',
        'reliability.mtbf_population', 'reliability.population_membership',
        'reliability.configuration_equivalence_review', 'reliability.mtbf_scope',
        'reliability.exposure_assessment', 'reliability.exposure_scope_assignment',
        'reliability.interruption', 'reliability.statistics_method',
        'reliability.current_mtbf_result', 'reliability.current_conclusion',
        'reliability.recompute_job', 'reliability.pending_item',
        'health.health_check_run', 'health.health_trigger_progress',
        'health.metric_observation', 'health.reference_cohort',
        'health.reference_membership', 'integration.import_batch',
        'integration.migration_issue', 'integration.artifact',
        'integration.retention_policy'
    ] LOOP
        EXECUTE format(
            'CREATE TRIGGER trg_set_updated_at BEFORE UPDATE ON %s '
            'FOR EACH ROW EXECUTE FUNCTION public.set_updated_at()',
            v_table
        );
    END LOOP;
END;
$$;

-- ---------------------------------------------------------------------------
-- Automatic audit, append-only protection and recompute queue
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION audit.capture_row_change()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, audit
AS $$
DECLARE
    v_before jsonb;
    v_after jsonb;
    v_identity text;
    v_actor bigint;
BEGIN
    IF TG_OP = 'INSERT' THEN
        v_after := to_jsonb(NEW);
    ELSIF TG_OP = 'UPDATE' THEN
        v_before := to_jsonb(OLD);
        v_after := to_jsonb(NEW);
    ELSE
        v_before := to_jsonb(OLD);
    END IF;

    v_identity := coalesce(
        v_after ->> 'public_id', v_before ->> 'public_id',
        v_after ->> 'id', v_before ->> 'id',
        v_after ->> 'asset_id', v_before ->> 'asset_id', ''
    );
    v_actor := nullif(current_setting('app.user_id', true), '')::bigint;

    INSERT INTO audit.change_log (
        actor_user_id, database_role, request_id, change_reason,
        operation, schema_name, table_name, row_identity,
        before_data, after_data, source_ip
    ) VALUES (
        v_actor,
        session_user,
        coalesce(current_setting('app.request_id', true), ''),
        coalesce(current_setting('app.change_reason', true), ''),
        TG_OP,
        TG_TABLE_SCHEMA,
        TG_TABLE_NAME,
        v_identity,
        v_before,
        v_after,
        nullif(current_setting('app.source_ip', true), '')::inet
    );

    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION audit.reject_change_log_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'audit.change_log is append-only';
END;
$$;

CREATE TRIGGER trg_change_log_no_update
BEFORE UPDATE ON audit.change_log
FOR EACH ROW EXECUTE FUNCTION audit.reject_change_log_mutation();

CREATE TRIGGER trg_change_log_no_delete
BEFORE DELETE ON audit.change_log
FOR EACH ROW EXECUTE FUNCTION audit.reject_change_log_mutation();

DO $$
DECLARE
    v_table text;
BEGIN
    FOREACH v_table IN ARRAY ARRAY[
        'iam.app_user', 'iam.user_campaign_access',
        'test.site', 'test.lab', 'test.station', 'test.manufacturing_batch',
        'test.asset', 'test.whole_machine_profile', 'test.module_profile',
        'test.asset_relationship', 'test.configuration_snapshot',
        'test.configuration_change', 'test.test_program', 'test.test_campaign',
        'test.campaign_asset', 'test.campaign_coverage_item', 'test.test_cycle',
        'test.analysis_segment', 'test.test_execution', 'test.execution_stage',
        'test.stage_mission_tag', 'test.runtime_interval', 'test.test_event',
        'catalog.mission_tag', 'catalog.test_case', 'catalog.test_case_version',
        'catalog.configuration_equivalence_rule', 'catalog.error_catalog_version',
        'catalog.error_code', 'catalog.source_error_mapping',
        'catalog.metric_definition', 'catalog.metric_version',
        'catalog.health_check_policy', 'catalog.health_check_trigger',
        'catalog.health_check_requirement',
        'reliability.mtbf_population', 'reliability.population_campaign',
        'reliability.population_membership',
        'reliability.configuration_equivalence_review',
        'reliability.mtbf_scope', 'reliability.mtbf_scope_rule',
        'reliability.exposure_assessment', 'reliability.exposure_scope_assignment',
        'reliability.interruption', 'reliability.interruption_error_code',
        'reliability.interruption_scope_assignment',
        'reliability.statistics_method', 'reliability.current_conclusion',
        'health.health_check_run', 'health.health_trigger_progress',
        'health.metric_observation', 'health.analysis_reference_point',
        'health.reference_cohort', 'health.reference_cohort_campaign',
        'health.reference_membership', 'integration.ingestion_source',
        'integration.import_batch', 'integration.legacy_id_map',
        'integration.migration_issue', 'integration.artifact',
        'integration.retention_policy', 'integration.evidence_hold'
    ] LOOP
        EXECUTE format(
            'CREATE TRIGGER trg_audit_row_change '
            'AFTER INSERT OR UPDATE OR DELETE ON %s '
            'FOR EACH ROW EXECUTE FUNCTION audit.capture_row_change()',
            v_table
        );
    END LOOP;
END;
$$;

CREATE OR REPLACE FUNCTION reliability.enqueue_recompute_from_change()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, reliability
AS $$
DECLARE
    v_data jsonb := coalesce(to_jsonb(NEW), to_jsonb(OLD));
    v_campaign_id bigint;
    v_identity text;
BEGIN
    v_campaign_id := nullif(v_data ->> 'campaign_id', '')::bigint;
    v_identity := coalesce(
        v_data ->> 'public_id', v_data ->> 'id', v_data ->> 'asset_id', 'unknown'
    );
    INSERT INTO reliability.recompute_job (
        campaign_id, resource_kind, resource_key, reason, payload
    ) VALUES (
        v_campaign_id,
        TG_TABLE_SCHEMA || '.' || TG_TABLE_NAME,
        TG_TABLE_SCHEMA || '.' || TG_TABLE_NAME || ':' || v_identity,
        TG_OP,
        jsonb_build_object('operation', TG_OP, 'row', v_data)
    );
    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END;
$$;

DO $$
DECLARE
    v_table text;
BEGIN
    FOREACH v_table IN ARRAY ARRAY[
        'test.configuration_snapshot', 'test.runtime_interval',
        'reliability.population_membership',
        'reliability.exposure_assessment',
        'reliability.exposure_scope_assignment',
        'reliability.interruption',
        'reliability.interruption_scope_assignment',
        'health.metric_observation'
    ] LOOP
        EXECUTE format(
            'CREATE TRIGGER trg_enqueue_recompute '
            'AFTER INSERT OR UPDATE ON %s '
            'FOR EACH ROW EXECUTE FUNCTION reliability.enqueue_recompute_from_change()',
            v_table
        );
    END LOOP;
END;
$$;

CREATE OR REPLACE FUNCTION reliability.claim_recompute_job(p_worker_id text)
RETURNS SETOF reliability.recompute_job
LANGUAGE sql
SECURITY DEFINER
SET search_path = pg_catalog, reliability
AS $$
    UPDATE reliability.recompute_job
    SET status = 'PROCESSING',
        attempts = attempts + 1,
        locked_by = p_worker_id,
        locked_at = clock_timestamp(),
        updated_at = clock_timestamp()
    WHERE id = (
        SELECT id
        FROM reliability.recompute_job
        WHERE status = 'PENDING'
          AND available_at <= clock_timestamp()
        ORDER BY requested_at, id
        FOR UPDATE SKIP LOCKED
        LIMIT 1
    )
    RETURNING *
$$;

-- ---------------------------------------------------------------------------
-- RLS policies
-- ---------------------------------------------------------------------------

ALTER TABLE iam.app_user ENABLE ROW LEVEL SECURITY;
CREATE POLICY app_user_select ON iam.app_user
    FOR SELECT USING (iam.is_authenticated());
CREATE POLICY app_user_admin_write ON iam.app_user
    FOR ALL USING (iam.is_system_admin()) WITH CHECK (iam.is_system_admin());

ALTER TABLE iam.user_campaign_access ENABLE ROW LEVEL SECURITY;
CREATE POLICY user_campaign_select ON iam.user_campaign_access
    FOR SELECT USING (iam.is_system_admin() OR user_id = iam.current_user_id());
CREATE POLICY user_campaign_admin_write ON iam.user_campaign_access
    FOR ALL USING (iam.is_system_admin()) WITH CHECK (iam.is_system_admin());

DO $$
DECLARE
    v_table text;
BEGIN
    FOREACH v_table IN ARRAY ARRAY[
        'test.site', 'test.lab', 'test.station', 'test.manufacturing_batch',
        'catalog.mission_tag', 'catalog.test_case', 'catalog.test_case_version',
        'catalog.configuration_equivalence_rule', 'catalog.error_catalog_version',
        'catalog.error_code', 'catalog.source_error_mapping',
        'catalog.metric_definition', 'catalog.metric_version',
        'catalog.health_check_policy', 'catalog.health_check_trigger',
        'catalog.health_check_requirement'
    ] LOOP
        EXECUTE format('ALTER TABLE %s ENABLE ROW LEVEL SECURITY', v_table);
        EXECUTE format(
            'CREATE POLICY authenticated_select ON %s FOR SELECT USING (iam.is_authenticated())',
            v_table
        );
        EXECUTE format(
            'CREATE POLICY executor_write ON %s FOR ALL '
            'USING (iam.can_write_business()) WITH CHECK (iam.can_write_business())',
            v_table
        );
    END LOOP;
END;
$$;

ALTER TABLE test.asset ENABLE ROW LEVEL SECURITY;
CREATE POLICY asset_select ON test.asset
    FOR SELECT USING (iam.can_access_asset(id, false));
CREATE POLICY asset_insert ON test.asset
    FOR INSERT WITH CHECK (iam.can_write_business());
CREATE POLICY asset_update ON test.asset
    FOR UPDATE USING (iam.can_access_asset(id, true))
    WITH CHECK (iam.can_access_asset(id, true));

DO $$
DECLARE
    v_table text;
BEGIN
    FOREACH v_table IN ARRAY ARRAY[
        'test.whole_machine_profile', 'test.module_profile', 'test.configuration_snapshot'
    ] LOOP
        EXECUTE format('ALTER TABLE %s ENABLE ROW LEVEL SECURITY', v_table);
        EXECUTE format(
            'CREATE POLICY asset_scoped_select ON %s FOR SELECT '
            'USING (iam.can_access_asset(asset_id, false))',
            v_table
        );
        EXECUTE format(
            'CREATE POLICY asset_scoped_write ON %s FOR ALL '
            'USING (iam.can_access_asset(asset_id, true)) '
            'WITH CHECK (iam.can_access_asset(asset_id, true))',
            v_table
        );
    END LOOP;
END;
$$;

ALTER TABLE test.asset_relationship ENABLE ROW LEVEL SECURITY;
CREATE POLICY relationship_select ON test.asset_relationship
    FOR SELECT USING (
        iam.can_access_asset(parent_asset_id, false) AND
        iam.can_access_asset(child_asset_id, false)
    );
CREATE POLICY relationship_write ON test.asset_relationship
    FOR ALL USING (
        iam.can_access_asset(parent_asset_id, true) AND
        iam.can_access_asset(child_asset_id, true)
    ) WITH CHECK (
        iam.can_access_asset(parent_asset_id, true) AND
        iam.can_access_asset(child_asset_id, true)
    );

ALTER TABLE test.test_program ENABLE ROW LEVEL SECURITY;
CREATE POLICY program_select ON test.test_program
    FOR SELECT USING (
        iam.is_system_admin() OR EXISTS (
            SELECT 1 FROM test.test_campaign c
            WHERE c.program_id = id AND iam.can_access_campaign(c.id, false)
        )
    );
CREATE POLICY program_write ON test.test_program
    FOR ALL USING (iam.can_write_business()) WITH CHECK (iam.can_write_business());

ALTER TABLE test.test_campaign ENABLE ROW LEVEL SECURITY;
CREATE POLICY campaign_select ON test.test_campaign
    FOR SELECT USING (iam.can_access_campaign(id, false));
CREATE POLICY campaign_insert ON test.test_campaign
    FOR INSERT WITH CHECK (iam.is_system_admin());
CREATE POLICY campaign_update ON test.test_campaign
    FOR UPDATE USING (iam.can_access_campaign(id, true))
    WITH CHECK (iam.can_access_campaign(id, true));

DO $$
DECLARE
    v_table text;
BEGIN
    FOREACH v_table IN ARRAY ARRAY[
        'test.campaign_asset', 'test.campaign_coverage_item',
        'test.test_cycle', 'test.analysis_segment', 'test.test_execution',
        'test.execution_stage', 'test.runtime_interval', 'test.test_event',
        'reliability.configuration_equivalence_review',
        'reliability.exposure_assessment',
        'reliability.exposure_scope_assignment',
        'reliability.interruption', 'reliability.pending_item',
        'health.health_check_run', 'health.metric_observation',
        'health.analysis_reference_point', 'integration.artifact'
    ] LOOP
        EXECUTE format('ALTER TABLE %s ENABLE ROW LEVEL SECURITY', v_table);
        EXECUTE format(
            'CREATE POLICY campaign_scoped_select ON %s FOR SELECT '
            'USING (iam.can_access_campaign(campaign_id, false))',
            v_table
        );
        EXECUTE format(
            'CREATE POLICY campaign_scoped_write ON %s FOR ALL '
            'USING (iam.can_access_campaign(campaign_id, true)) '
            'WITH CHECK (iam.can_access_campaign(campaign_id, true))',
            v_table
        );
    END LOOP;
END;
$$;

ALTER TABLE test.configuration_change ENABLE ROW LEVEL SECURITY;
CREATE POLICY configuration_change_select ON test.configuration_change
    FOR SELECT USING (iam.can_access_asset(asset_id, false));
CREATE POLICY configuration_change_write ON test.configuration_change
    FOR ALL USING (iam.can_access_asset(asset_id, true))
    WITH CHECK (iam.can_access_asset(asset_id, true));

ALTER TABLE test.stage_mission_tag ENABLE ROW LEVEL SECURITY;
CREATE POLICY stage_tag_select ON test.stage_mission_tag
    FOR SELECT USING (EXISTS (
        SELECT 1 FROM test.execution_stage es
        WHERE es.id = stage_id AND iam.can_access_campaign(es.campaign_id, false)
    ));
CREATE POLICY stage_tag_write ON test.stage_mission_tag
    FOR ALL USING (EXISTS (
        SELECT 1 FROM test.execution_stage es
        WHERE es.id = stage_id AND iam.can_access_campaign(es.campaign_id, true)
    )) WITH CHECK (EXISTS (
        SELECT 1 FROM test.execution_stage es
        WHERE es.id = stage_id AND iam.can_access_campaign(es.campaign_id, true)
    ));

DO $$
DECLARE
    v_table text;
BEGIN
    FOREACH v_table IN ARRAY ARRAY[
        'reliability.mtbf_population', 'reliability.population_membership'
    ] LOOP
        EXECUTE format('ALTER TABLE %s ENABLE ROW LEVEL SECURITY', v_table);
    END LOOP;
END;
$$;

CREATE POLICY population_select ON reliability.mtbf_population
    FOR SELECT USING (iam.can_access_population(id, false));
CREATE POLICY population_insert ON reliability.mtbf_population
    FOR INSERT WITH CHECK (iam.can_write_business());
CREATE POLICY population_update ON reliability.mtbf_population
    FOR UPDATE USING (iam.can_access_population(id, true))
    WITH CHECK (iam.can_access_population(id, true));

ALTER TABLE reliability.population_campaign ENABLE ROW LEVEL SECURITY;
CREATE POLICY population_campaign_select ON reliability.population_campaign
    FOR SELECT USING (iam.can_access_campaign(campaign_id, false));
CREATE POLICY population_campaign_write ON reliability.population_campaign
    FOR ALL USING (iam.can_access_campaign(campaign_id, true))
    WITH CHECK (iam.can_access_campaign(campaign_id, true));

CREATE POLICY population_membership_select ON reliability.population_membership
    FOR SELECT USING (iam.can_access_population(population_id, false));
CREATE POLICY population_membership_write ON reliability.population_membership
    FOR ALL USING (iam.can_access_population(population_id, true))
    WITH CHECK (iam.can_access_population(population_id, true));

DO $$
DECLARE
    v_table text;
BEGIN
    FOREACH v_table IN ARRAY ARRAY[
        'reliability.mtbf_scope', 'reliability.mtbf_scope_rule',
        'reliability.statistics_method'
    ] LOOP
        EXECUTE format('ALTER TABLE %s ENABLE ROW LEVEL SECURITY', v_table);
        EXECUTE format(
            'CREATE POLICY reliability_catalog_select ON %s FOR SELECT USING (iam.is_authenticated())',
            v_table
        );
        EXECUTE format(
            'CREATE POLICY reliability_catalog_admin_write ON %s FOR ALL '
            'USING (iam.is_system_admin()) WITH CHECK (iam.is_system_admin())',
            v_table
        );
    END LOOP;
END;
$$;

ALTER TABLE reliability.calculation_run ENABLE ROW LEVEL SECURITY;
CREATE POLICY calculation_select ON reliability.calculation_run
    FOR SELECT USING (iam.can_access_calculation(id, false));
CREATE POLICY calculation_admin_write ON reliability.calculation_run
    FOR ALL USING (iam.is_system_admin()) WITH CHECK (iam.is_system_admin());

ALTER TABLE reliability.calculation_contributor_campaign ENABLE ROW LEVEL SECURITY;
CREATE POLICY calculation_campaign_select ON reliability.calculation_contributor_campaign
    FOR SELECT USING (iam.can_access_campaign(campaign_id, false));
CREATE POLICY calculation_campaign_admin_write ON reliability.calculation_contributor_campaign
    FOR ALL USING (iam.is_system_admin()) WITH CHECK (iam.is_system_admin());

DO $$
DECLARE
    v_table text;
BEGIN
    FOREACH v_table IN ARRAY ARRAY[
        'reliability.current_mtbf_result', 'reliability.current_conclusion'
    ] LOOP
        EXECUTE format('ALTER TABLE %s ENABLE ROW LEVEL SECURITY', v_table);
        EXECUTE format(
            'CREATE POLICY calculation_result_select ON %s FOR SELECT '
            'USING (iam.can_access_calculation(calculation_run_id, false))',
            v_table
        );
        EXECUTE format(
            'CREATE POLICY calculation_result_admin_write ON %s FOR ALL '
            'USING (iam.is_system_admin()) WITH CHECK (iam.is_system_admin())',
            v_table
        );
    END LOOP;
END;
$$;

ALTER TABLE reliability.recompute_job ENABLE ROW LEVEL SECURITY;
CREATE POLICY recompute_admin ON reliability.recompute_job
    FOR ALL USING (iam.is_system_admin()) WITH CHECK (iam.is_system_admin());

DO $$
DECLARE
    v_table text;
BEGIN
    FOREACH v_table IN ARRAY ARRAY[
        'reliability.interruption_error_code',
        'reliability.interruption_scope_assignment'
    ] LOOP
        EXECUTE format('ALTER TABLE %s ENABLE ROW LEVEL SECURITY', v_table);
    END LOOP;
END;
$$;

CREATE POLICY interruption_code_select ON reliability.interruption_error_code
    FOR SELECT USING (EXISTS (
        SELECT 1 FROM reliability.interruption i
        WHERE i.id = interruption_id AND iam.can_access_campaign(i.campaign_id, false)
    ));
CREATE POLICY interruption_code_write ON reliability.interruption_error_code
    FOR ALL USING (EXISTS (
        SELECT 1 FROM reliability.interruption i
        WHERE i.id = interruption_id AND iam.can_access_campaign(i.campaign_id, true)
    )) WITH CHECK (EXISTS (
        SELECT 1 FROM reliability.interruption i
        WHERE i.id = interruption_id AND iam.can_access_campaign(i.campaign_id, true)
    ));

CREATE POLICY interruption_scope_select ON reliability.interruption_scope_assignment
    FOR SELECT USING (EXISTS (
        SELECT 1 FROM reliability.interruption i
        WHERE i.id = interruption_id AND iam.can_access_campaign(i.campaign_id, false)
    ));
CREATE POLICY interruption_scope_write ON reliability.interruption_scope_assignment
    FOR ALL USING (EXISTS (
        SELECT 1 FROM reliability.interruption i
        WHERE i.id = interruption_id AND iam.can_access_campaign(i.campaign_id, true)
    )) WITH CHECK (EXISTS (
        SELECT 1 FROM reliability.interruption i
        WHERE i.id = interruption_id AND iam.can_access_campaign(i.campaign_id, true)
    ));

ALTER TABLE health.reference_cohort ENABLE ROW LEVEL SECURITY;
CREATE POLICY cohort_select ON health.reference_cohort
    FOR SELECT USING (iam.can_access_cohort(id, false));
CREATE POLICY cohort_insert ON health.reference_cohort
    FOR INSERT WITH CHECK (iam.can_write_business());
CREATE POLICY cohort_update ON health.reference_cohort
    FOR UPDATE USING (iam.can_access_cohort(id, true))
    WITH CHECK (iam.can_access_cohort(id, true));

ALTER TABLE health.reference_cohort_campaign ENABLE ROW LEVEL SECURITY;
CREATE POLICY cohort_campaign_select ON health.reference_cohort_campaign
    FOR SELECT USING (iam.can_access_campaign(campaign_id, false));
CREATE POLICY cohort_campaign_write ON health.reference_cohort_campaign
    FOR ALL USING (iam.can_access_campaign(campaign_id, true))
    WITH CHECK (iam.can_access_campaign(campaign_id, true));

ALTER TABLE health.reference_membership ENABLE ROW LEVEL SECURITY;
CREATE POLICY cohort_membership_select ON health.reference_membership
    FOR SELECT USING (iam.can_access_cohort(cohort_id, false));
CREATE POLICY cohort_membership_write ON health.reference_membership
    FOR ALL USING (iam.can_access_cohort(cohort_id, true))
    WITH CHECK (iam.can_access_cohort(cohort_id, true));

ALTER TABLE health.health_trigger_progress ENABLE ROW LEVEL SECURITY;
CREATE POLICY trigger_progress_select ON health.health_trigger_progress
    FOR SELECT USING (EXISTS (
        SELECT 1 FROM health.health_check_run hcr
        WHERE hcr.id = health_check_run_id
          AND iam.can_access_campaign(hcr.campaign_id, false)
    ));
CREATE POLICY trigger_progress_write ON health.health_trigger_progress
    FOR ALL USING (EXISTS (
        SELECT 1 FROM health.health_check_run hcr
        WHERE hcr.id = health_check_run_id
          AND iam.can_access_campaign(hcr.campaign_id, true)
    )) WITH CHECK (EXISTS (
        SELECT 1 FROM health.health_check_run hcr
        WHERE hcr.id = health_check_run_id
          AND iam.can_access_campaign(hcr.campaign_id, true)
    ));

DO $$
DECLARE
    v_table text;
BEGIN
    FOREACH v_table IN ARRAY ARRAY[
        'integration.ingestion_source', 'integration.ingestion_receipt',
        'integration.import_batch', 'integration.legacy_id_map',
        'integration.migration_issue', 'integration.retention_policy',
        'integration.evidence_hold'
    ] LOOP
        EXECUTE format('ALTER TABLE %s ENABLE ROW LEVEL SECURITY', v_table);
        EXECUTE format(
            'CREATE POLICY integration_admin ON %s FOR ALL '
            'USING (iam.is_system_admin()) WITH CHECK (iam.is_system_admin())',
            v_table
        );
    END LOOP;
END;
$$;

ALTER TABLE audit.change_log ENABLE ROW LEVEL SECURITY;
CREATE POLICY audit_admin_select ON audit.change_log
    FOR SELECT USING (iam.is_system_admin());

-- ---------------------------------------------------------------------------
-- Grants. RLS remains active for application and read-only roles.
-- ---------------------------------------------------------------------------

GRANT USAGE ON SCHEMA iam, catalog, test, reliability, health, integration, audit
TO __RP1_APP_USER__, __RP1_READONLY_USER__;

GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA iam, catalog, test, reliability, health, integration
TO __RP1_APP_USER__;
GRANT SELECT ON ALL TABLES IN SCHEMA audit TO __RP1_APP_USER__;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA iam, catalog, test, reliability, health, integration
TO __RP1_APP_USER__;

REVOKE ALL ON iam.user_credential, iam.auth_session FROM __RP1_APP_USER__, __RP1_READONLY_USER__;
REVOKE INSERT, UPDATE, DELETE ON audit.change_log FROM __RP1_APP_USER__, __RP1_READONLY_USER__;

GRANT SELECT ON ALL TABLES IN SCHEMA iam, catalog, test, reliability, health, integration, audit
TO __RP1_READONLY_USER__;

GRANT EXECUTE ON FUNCTION iam.set_request_context(uuid, text, text)
TO __RP1_APP_USER__, __RP1_READONLY_USER__;
GRANT EXECUTE ON FUNCTION reliability.claim_recompute_job(text)
TO __RP1_APP_USER__;

REVOKE ALL ON FUNCTION audit.capture_row_change() FROM PUBLIC;
REVOKE ALL ON FUNCTION reliability.enqueue_recompute_from_change() FROM PUBLIC;
