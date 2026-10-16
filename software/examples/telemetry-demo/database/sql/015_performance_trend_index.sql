CREATE INDEX idx_observation_asset_execution_metric_stage_time
    ON health.metric_observation(
        asset_id,
        execution_id,
        metric_version_id,
        stage_id,
        observed_at,
        id
    )
    WHERE NOT voided AND canonical_numeric_value IS NOT NULL;
