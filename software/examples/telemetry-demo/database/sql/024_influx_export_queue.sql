-- Influx is a projection: the API transaction stores both facts and export work.
ALTER TABLE test.test_execution
    ADD COLUMN telemetry_source_kind text NOT NULL DEFAULT 'real'
    CHECK (telemetry_source_kind IN ('real', 'synthetic'));

CREATE TABLE integration.influx_export_queue (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    batch_id bigint NOT NULL UNIQUE REFERENCES integration.ingestion_batch(id),
    source_id bigint NOT NULL REFERENCES integration.ingestion_source(id),
    execution_id bigint NOT NULL REFERENCES test.test_execution(id),
    line_protocol text NOT NULL CHECK (btrim(line_protocol) <> ''),
    point_count integer NOT NULL CHECK (point_count > 0),
    status text NOT NULL DEFAULT 'PENDING' CHECK (status IN ('PENDING', 'SENDING', 'SENT')),
    attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    available_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    locked_by text,
    locked_until timestamptz,
    last_error text NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    sent_at timestamptz,
    CHECK ((locked_by IS NULL) = (locked_until IS NULL))
);
CREATE INDEX ix_influx_queue_available ON integration.influx_export_queue(available_at, id)
    WHERE status <> 'SENT';
ALTER TABLE integration.influx_export_queue ENABLE ROW LEVEL SECURITY;
ALTER TABLE integration.influx_export_queue FORCE ROW LEVEL SECURITY;
CREATE POLICY influx_enqueue ON integration.influx_export_queue FOR INSERT
    WITH CHECK (
        iam.can_ingest_source(source_id)
        AND EXISTS (
            SELECT 1 FROM test.test_execution execution
            JOIN integration.ingestion_batch batch ON batch.execution_id = execution.id
            WHERE execution.id = influx_export_queue.execution_id
              AND execution.source_id = influx_export_queue.source_id
              AND batch.id = influx_export_queue.batch_id
              AND batch.source_id = influx_export_queue.source_id
        )
    );
CREATE POLICY influx_worker_select ON integration.influx_export_queue FOR SELECT
    USING (iam.current_user_id() = (
        SELECT id FROM iam.app_user WHERE username = 'rp1-recompute-worker'
    ));
CREATE POLICY influx_worker_update ON integration.influx_export_queue FOR UPDATE
    USING (iam.current_user_id() = (
        SELECT id FROM iam.app_user WHERE username = 'rp1-recompute-worker'
    ))
    WITH CHECK (iam.current_user_id() = (
        SELECT id FROM iam.app_user WHERE username = 'rp1-recompute-worker'
    ));
GRANT SELECT, INSERT, UPDATE ON integration.influx_export_queue TO __RP1_APP_USER__;
GRANT USAGE, SELECT ON SEQUENCE integration.influx_export_queue_id_seq TO __RP1_APP_USER__;
REVOKE ALL ON integration.influx_export_queue FROM __RP1_READONLY_USER__;
COMMENT ON TABLE integration.influx_export_queue IS
    'Durable at-least-once export. Equal Influx tags/timestamps/fields make retries idempotent. Raw samples remain external.';
