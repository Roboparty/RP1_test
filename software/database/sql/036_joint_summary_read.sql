-- Let operators who can already open an execution read its unreviewed joint
-- summaries. analysis_eligible stays false; these rows are not formal metrics.

CREATE POLICY historical_recovery_campaign_read
    ON integration.legacy_recovered_execution
    FOR SELECT
    USING (
        existing_execution_id IS NOT NULL
        AND EXISTS (
            SELECT 1
            FROM test.test_execution execution
            WHERE execution.id = legacy_recovered_execution.existing_execution_id
              AND iam.can_access_campaign(execution.campaign_id, false)
        )
    );

CREATE POLICY historical_recovery_campaign_read
    ON integration.legacy_joint_summary
    FOR SELECT
    USING (
        EXISTS (
            SELECT 1
            FROM integration.legacy_recovered_execution recovered
            JOIN test.test_execution execution
              ON execution.id = recovered.existing_execution_id
            WHERE recovered.batch_key = legacy_joint_summary.batch_key
              AND recovered.source_test_id = legacy_joint_summary.source_test_id
              AND iam.can_access_campaign(execution.campaign_id, false)
        )
    );
