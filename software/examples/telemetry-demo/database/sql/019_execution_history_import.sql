CREATE TABLE test.execution_result (
    execution_id bigint PRIMARY KEY REFERENCES test.test_execution(id),
    source_status text NOT NULL CHECK (
        source_status IN ('passed', 'failed', 'blocked', 'scheduled', 'running')
    ),
    outcome text NOT NULL CHECK (
        outcome IN ('PASSED', 'FAILED', 'INCONCLUSIVE', 'NOT_EVALUATED')
    ),
    termination_kind text NOT NULL CHECK (
        termination_kind IN (
            'NORMAL',
            'MANUAL_STOP',
            'SAFETY_WATCHDOG',
            'DATA_TIMEOUT',
            'SYSTEM_CRASH',
            'SCHEDULED',
            'RUNNING',
            'UNKNOWN'
        )
    ),
    summary text NOT NULL DEFAULT '',
    issues text NOT NULL DEFAULT '',
    exception_count integer NOT NULL DEFAULT 0 CHECK (exception_count >= 0),
    reported_duration_seconds numeric(20,6) NOT NULL
        CHECK (reported_duration_seconds >= 0),
    executor_display text NOT NULL DEFAULT '',
    environment_label text NOT NULL DEFAULT '',
    last_data_at timestamptz,
    telemetry_source text NOT NULL DEFAULT '',
    archive_status text NOT NULL CHECK (
        archive_status IN ('PENDING', 'IMPORTED', 'ARCHIVED', 'MISSING')
    ),
    archive_path text NOT NULL DEFAULT '',
    report_reference text NOT NULL DEFAULT '',
    normalization_flags jsonb NOT NULL DEFAULT '[]'::jsonb,
    source_created_at timestamptz NOT NULL,
    source_updated_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK (jsonb_typeof(normalization_flags) = 'array'),
    CHECK (source_updated_at >= source_created_at)
);

CREATE INDEX ix_execution_result_outcome_termination
    ON test.execution_result(outcome, termination_kind, execution_id);
CREATE INDEX ix_execution_result_normalization_flags
    ON test.execution_result USING gin(normalization_flags);

CREATE TABLE integration.execution_import_record (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_id bigint NOT NULL REFERENCES integration.ingestion_source(id),
    source_execution_key text NOT NULL,
    execution_id bigint NOT NULL UNIQUE REFERENCES test.test_execution(id),
    source_file text NOT NULL,
    source_row_number integer NOT NULL CHECK (source_row_number >= 2),
    row_hash text NOT NULL CHECK (row_hash ~ '^[0-9a-f]{64}$'),
    raw_payload jsonb NOT NULL,
    normalization_warnings jsonb NOT NULL DEFAULT '[]'::jsonb,
    imported_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (source_id, source_execution_key),
    CHECK (btrim(source_execution_key) <> ''),
    CHECK (btrim(source_file) <> ''),
    CHECK (jsonb_typeof(raw_payload) = 'object'),
    CHECK (jsonb_typeof(normalization_warnings) = 'array')
);

CREATE INDEX ix_execution_import_record_source_file_row
    ON integration.execution_import_record(source_id, source_file, source_row_number);
CREATE INDEX ix_execution_import_record_warnings
    ON integration.execution_import_record USING gin(normalization_warnings);

CREATE UNIQUE INDEX uq_artifact_legacy_execution_report
    ON integration.artifact(execution_id, kind)
    WHERE execution_id IS NOT NULL
      AND kind = 'LEGACY_REPORT_REFERENCE';

CREATE TRIGGER trg_set_updated_at
BEFORE UPDATE ON test.execution_result
FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();

CREATE TRIGGER trg_audit_row_change
AFTER INSERT OR UPDATE OR DELETE ON test.execution_result
FOR EACH ROW EXECUTE FUNCTION audit.capture_row_change();

CREATE TRIGGER trg_audit_row_change
AFTER INSERT OR UPDATE OR DELETE ON integration.execution_import_record
FOR EACH ROW EXECUTE FUNCTION audit.capture_row_change();

ALTER TABLE test.execution_result ENABLE ROW LEVEL SECURITY;
CREATE POLICY campaign_scoped_select ON test.execution_result
    FOR SELECT USING (
        EXISTS (
            SELECT 1
            FROM test.test_execution execution
            WHERE execution.id = execution_id
              AND iam.can_access_campaign(execution.campaign_id, false)
        )
    );
CREATE POLICY campaign_scoped_write ON test.execution_result
    FOR ALL
    USING (
        EXISTS (
            SELECT 1
            FROM test.test_execution execution
            WHERE execution.id = execution_id
              AND iam.can_access_campaign(execution.campaign_id, true)
        )
    )
    WITH CHECK (
        EXISTS (
            SELECT 1
            FROM test.test_execution execution
            WHERE execution.id = execution_id
              AND iam.can_access_campaign(execution.campaign_id, true)
        )
    );

ALTER TABLE integration.execution_import_record ENABLE ROW LEVEL SECURITY;
CREATE POLICY campaign_scoped_select ON integration.execution_import_record
    FOR SELECT USING (
        EXISTS (
            SELECT 1
            FROM test.test_execution execution
            WHERE execution.id = execution_id
              AND iam.can_access_campaign(execution.campaign_id, false)
        )
    );
CREATE POLICY campaign_scoped_write ON integration.execution_import_record
    FOR ALL
    USING (
        EXISTS (
            SELECT 1
            FROM test.test_execution execution
            WHERE execution.id = execution_id
              AND iam.can_access_campaign(execution.campaign_id, true)
        )
    )
    WITH CHECK (
        EXISTS (
            SELECT 1
            FROM test.test_execution execution
            WHERE execution.id = execution_id
              AND iam.can_access_campaign(execution.campaign_id, true)
        )
    );

GRANT SELECT, INSERT, UPDATE ON test.execution_result TO __RP1_APP_USER__;
GRANT SELECT ON test.execution_result TO __RP1_READONLY_USER__;

GRANT SELECT, INSERT, UPDATE ON integration.execution_import_record
TO __RP1_APP_USER__;
GRANT SELECT ON integration.execution_import_record TO __RP1_READONLY_USER__;
GRANT USAGE, SELECT
ON SEQUENCE integration.execution_import_record_id_seq
TO __RP1_APP_USER__;

COMMENT ON TABLE test.execution_result IS
    'One-to-one normalized outcome and legacy execution metadata.';
COMMENT ON COLUMN test.execution_result.normalization_flags IS
    'Structured warnings produced while normalizing the immutable source row.';
COMMENT ON TABLE integration.execution_import_record IS
    'Idempotent execution CSV lineage with raw source payload and row hash.';
COMMENT ON INDEX integration.uq_artifact_legacy_execution_report IS
    'Allows one idempotently updated legacy report reference per execution.';
