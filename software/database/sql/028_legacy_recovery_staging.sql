-- Owner-run historical recovery extension. This does not approve or publish data.
-- Safe to apply independently on 0024, then again during a later normal upgrade.
CREATE TABLE IF NOT EXISTS integration.legacy_recovery_batch (
    batch_key text PRIMARY KEY,
    dataset_sha256 text NOT NULL CHECK (dataset_sha256 ~ '^[0-9a-f]{64}$'),
    schema_sha256 text NOT NULL CHECK (schema_sha256 ~ '^[0-9a-f]{64}$'),
    actor_user_id bigint NOT NULL REFERENCES iam.app_user(id),
    source_note text NOT NULL,
    counts jsonb NOT NULL CHECK (jsonb_typeof(counts) = 'object'),
    review_state text NOT NULL DEFAULT 'UNREVIEWED' CHECK (review_state = 'UNREVIEWED'),
    analysis_eligible boolean NOT NULL DEFAULT false CHECK (NOT analysis_eligible),
    imported_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE IF NOT EXISTS integration.legacy_recovered_execution (
    batch_key text NOT NULL REFERENCES integration.legacy_recovery_batch(batch_key),
    source_test_id text NOT NULL CHECK (length(source_test_id) BETWEEN 1 AND 500),
    existing_execution_id bigint REFERENCES test.test_execution(id),
    mapping_status text NOT NULL CHECK (mapping_status IN ('EXACT_EXISTING', 'MISSING_EXECUTION', 'UNMAPPED')),
    source_snapshot jsonb NOT NULL CHECK (
        jsonb_typeof(source_snapshot) = 'object' AND octet_length(source_snapshot::text) <= 16384
        AND NOT jsonb_path_exists(source_snapshot, 'strict $.** ? (@.type() == "array")')
    ),
    review_state text NOT NULL DEFAULT 'UNREVIEWED' CHECK (review_state = 'UNREVIEWED'),
    analysis_eligible boolean NOT NULL DEFAULT false CHECK (NOT analysis_eligible),
    row_sha256 text NOT NULL CHECK (row_sha256 ~ '^[0-9a-f]{64}$'),
    PRIMARY KEY (batch_key, source_test_id),
    CHECK ((mapping_status = 'EXACT_EXISTING') = (existing_execution_id IS NOT NULL))
);

CREATE TABLE IF NOT EXISTS integration.legacy_joint_summary (
    batch_key text NOT NULL,
    source_test_id text NOT NULL,
    source_kind text NOT NULL CHECK (source_kind IN ('POSTGRES_SUMMARY', 'INFLUX_AGGREGATE')),
    source_joint text NOT NULL CHECK (length(source_joint) BETWEEN 1 AND 100),
    -- Proposed explicit mapping, not an assertion that the current catalog supports it.
    proposed_subject_code text,
    source_robot_id text,
    source_limb text,
    first_observed_at timestamptz,
    last_observed_at timestamptz,
    mean_abs_torque_nm double precision,
    peak_abs_torque_nm double precision,
    temp_start_c double precision,
    temp_end_c double precision,
    temp_rise_c double precision,
    temp_max_c double precision,
    tracking_rms_rad double precision,
    bus_voltage_mean_v double precision,
    bus_voltage_min_v double precision,
    bus_voltage_max_v double precision,
    bus_current_mean_abs_a double precision,
    bus_current_peak_abs_a double precision,
    sample_count bigint NOT NULL CHECK (sample_count > 0),
    error_sample_count bigint CHECK (error_sample_count >= 0),
    provenance jsonb NOT NULL CHECK (
        jsonb_typeof(provenance) = 'object' AND octet_length(provenance::text) <= 4096
        AND NOT jsonb_path_exists(provenance, 'strict $.** ? (@.type() == "array")')
    ),
    review_state text NOT NULL DEFAULT 'UNREVIEWED' CHECK (review_state = 'UNREVIEWED'),
    analysis_eligible boolean NOT NULL DEFAULT false CHECK (NOT analysis_eligible),
    row_sha256 text NOT NULL CHECK (row_sha256 ~ '^[0-9a-f]{64}$'),
    PRIMARY KEY (batch_key, source_test_id, source_kind, source_joint),
    FOREIGN KEY (batch_key, source_test_id)
        REFERENCES integration.legacy_recovered_execution(batch_key, source_test_id),
    CHECK (last_observed_at IS NULL OR last_observed_at >= first_observed_at)
);

CREATE TABLE IF NOT EXISTS integration.legacy_recovered_event (
    batch_key text NOT NULL,
    source_test_id text NOT NULL,
    event_type text NOT NULL CHECK (event_type IN ('start', 'stop', 'fault', 'controller_timeout')),
    observed_at timestamptz NOT NULL,
    source_joint text,
    source_robot_id text,
    source_limb text,
    error_id double precision,
    duration_s double precision,
    elapsed_s double precision,
    fault_count double precision,
    review_state text NOT NULL DEFAULT 'UNREVIEWED' CHECK (review_state = 'UNREVIEWED'),
    analysis_eligible boolean NOT NULL DEFAULT false CHECK (NOT analysis_eligible),
    row_sha256 text NOT NULL CHECK (row_sha256 ~ '^[0-9a-f]{64}$'),
    PRIMARY KEY (batch_key, row_sha256),
    FOREIGN KEY (batch_key, source_test_id)
        REFERENCES integration.legacy_recovered_execution(batch_key, source_test_id)
);

CREATE OR REPLACE FUNCTION integration.reject_legacy_recovery_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'Historical source evidence is immutable; corrections require a new recovery batch';
END $$;
REVOKE ALL ON FUNCTION integration.reject_legacy_recovery_mutation() FROM PUBLIC;

DO $$
DECLARE t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['legacy_recovery_batch', 'legacy_recovered_execution',
                            'legacy_joint_summary', 'legacy_recovered_event'] LOOP
        EXECUTE format('ALTER TABLE integration.%I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('REVOKE ALL ON integration.%I FROM PUBLIC, __RP1_APP_USER__, __RP1_READONLY_USER__', t);
        EXECUTE format('GRANT SELECT ON integration.%I TO __RP1_APP_USER__, __RP1_READONLY_USER__', t);
        IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE schemaname='integration'
                       AND tablename=t AND policyname='historical_recovery_admin_read') THEN
            EXECUTE format('CREATE POLICY historical_recovery_admin_read ON integration.%I '
                           'FOR SELECT USING (iam.is_system_admin())', t);
        END IF;
        IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgrelid=format('integration.%I',t)::regclass
                       AND tgname='trg_legacy_recovery_audit') THEN
            EXECUTE format('CREATE TRIGGER trg_legacy_recovery_audit AFTER INSERT OR UPDATE OR DELETE '
                           'ON integration.%I FOR EACH ROW EXECUTE FUNCTION audit.capture_row_change()', t);
        END IF;
        IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgrelid=format('integration.%I',t)::regclass
                       AND tgname='trg_legacy_recovery_immutable') THEN
            EXECUTE format('CREATE TRIGGER trg_legacy_recovery_immutable BEFORE UPDATE OR DELETE '
                           'ON integration.%I FOR EACH ROW EXECUTE FUNCTION integration.reject_legacy_recovery_mutation()', t);
        END IF;
    END LOOP;
END $$;

COMMENT ON TABLE integration.legacy_joint_summary IS
    'Unreviewed historical scalar summaries. Never a formal metric, failure, or qualified exposure. Both sources retained without overwriting.';
COMMENT ON COLUMN integration.legacy_joint_summary.error_sample_count IS
    'Number of source error samples (old PostgreSQL error_count preserved); not independent failure events.';
