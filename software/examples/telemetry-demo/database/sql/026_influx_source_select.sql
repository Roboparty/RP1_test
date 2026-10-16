-- INSERT ... ON CONFLICT requires the candidate row to satisfy SELECT policies.
-- Collectors may inspect only their own source's durable projection work.
-- UPDATE/acknowledgement remains restricted to the internal worker identity.
CREATE POLICY influx_source_select ON integration.influx_export_queue FOR SELECT
    USING (source_id = iam.current_ingestion_source_id());
