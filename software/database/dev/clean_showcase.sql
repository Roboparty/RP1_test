\set ON_ERROR_STOP on

BEGIN;

SELECT iam.set_request_context(
    '00000000-0000-7000-8000-000000000002'::uuid,
    'showcase-clean',
    'manual development showcase cleanup'
);

CREATE TEMP TABLE showcase_campaign_ids ON COMMIT DROP AS
SELECT id FROM test.test_campaign WHERE campaign_code LIKE 'SHOWCASE-%';

CREATE TEMP TABLE showcase_asset_ids ON COMMIT DROP AS
SELECT id
FROM test.asset
WHERE product_family = 'RP1-SHOWCASE'
  AND notes = 'SHOWCASE development data';

CREATE TEMP TABLE showcase_execution_ids ON COMMIT DROP AS
SELECT id FROM test.test_execution WHERE execution_code LIKE 'SHOWCASE-%';

CREATE TEMP TABLE showcase_population_ids ON COMMIT DROP AS
SELECT id FROM reliability.mtbf_population WHERE population_code LIKE 'SHOWCASE-%';

DELETE FROM integration.artifact
WHERE object_key LIKE 'showcase/%'
   OR campaign_id IN (SELECT id FROM showcase_campaign_ids);

DELETE FROM health.analysis_reference_point
WHERE metric_observation_id IN (
    SELECT id FROM health.metric_observation
    WHERE calculation_run_key LIKE 'SHOWCASE-%'
);
DELETE FROM health.metric_observation
WHERE calculation_run_key LIKE 'SHOWCASE-%'
   OR campaign_id IN (SELECT id FROM showcase_campaign_ids);
DELETE FROM health.metric_series
WHERE source_series_key LIKE 'SHOWCASE-TELEMETRY-%'
   OR campaign_id IN (SELECT id FROM showcase_campaign_ids);
DELETE FROM health.health_check_run
WHERE campaign_id IN (SELECT id FROM showcase_campaign_ids);

DELETE FROM reliability.interruption_scope_assignment
WHERE interruption_id IN (
    SELECT id FROM reliability.interruption
    WHERE campaign_id IN (SELECT id FROM showcase_campaign_ids)
);
DELETE FROM reliability.interruption_error_code
WHERE interruption_id IN (
    SELECT id FROM reliability.interruption
    WHERE campaign_id IN (SELECT id FROM showcase_campaign_ids)
);
DELETE FROM reliability.interruption
WHERE campaign_id IN (SELECT id FROM showcase_campaign_ids);

DELETE FROM reliability.exposure_scope_assignment
WHERE campaign_id IN (SELECT id FROM showcase_campaign_ids);
DELETE FROM reliability.exposure_assessment
WHERE campaign_id IN (SELECT id FROM showcase_campaign_ids);

DELETE FROM test.test_event
WHERE campaign_id IN (SELECT id FROM showcase_campaign_ids);
DELETE FROM test.runtime_interval
WHERE campaign_id IN (SELECT id FROM showcase_campaign_ids);
DELETE FROM test.execution_stage
WHERE campaign_id IN (SELECT id FROM showcase_campaign_ids);
DELETE FROM test.test_execution
WHERE id IN (SELECT id FROM showcase_execution_ids)
   OR campaign_id IN (SELECT id FROM showcase_campaign_ids);
DELETE FROM test.analysis_segment
WHERE campaign_id IN (SELECT id FROM showcase_campaign_ids);
DELETE FROM test.test_cycle
WHERE campaign_id IN (SELECT id FROM showcase_campaign_ids);

DELETE FROM reliability.current_conclusion
WHERE population_id IN (SELECT id FROM showcase_population_ids);
DELETE FROM reliability.current_mtbf_result
WHERE campaign_id IN (SELECT id FROM showcase_campaign_ids)
   OR population_id IN (SELECT id FROM showcase_population_ids);
DELETE FROM reliability.calculation_contributor_campaign
WHERE campaign_id IN (SELECT id FROM showcase_campaign_ids);
DELETE FROM reliability.recompute_job
WHERE campaign_id IN (SELECT id FROM showcase_campaign_ids);
DELETE FROM reliability.calculation_run
WHERE campaign_id IN (SELECT id FROM showcase_campaign_ids)
   OR population_id IN (SELECT id FROM showcase_population_ids);
DELETE FROM reliability.campaign_mtbf_config
WHERE campaign_id IN (SELECT id FROM showcase_campaign_ids);
DELETE FROM reliability.recompute_job
WHERE campaign_id IN (SELECT id FROM showcase_campaign_ids);
DELETE FROM reliability.population_membership
WHERE population_id IN (SELECT id FROM showcase_population_ids);
DELETE FROM reliability.population_campaign
WHERE population_id IN (SELECT id FROM showcase_population_ids)
   OR campaign_id IN (SELECT id FROM showcase_campaign_ids);
DELETE FROM reliability.mtbf_population
WHERE id IN (SELECT id FROM showcase_population_ids);

DELETE FROM iam.user_campaign_access
WHERE campaign_id IN (SELECT id FROM showcase_campaign_ids);
DELETE FROM test.campaign_asset
WHERE campaign_id IN (SELECT id FROM showcase_campaign_ids);
DELETE FROM test.test_campaign
WHERE id IN (SELECT id FROM showcase_campaign_ids);

DELETE FROM test.module_profile
WHERE asset_id IN (SELECT id FROM showcase_asset_ids);
DELETE FROM test.whole_machine_profile
WHERE asset_id IN (SELECT id FROM showcase_asset_ids);
DELETE FROM test.configuration_snapshot
WHERE asset_id IN (SELECT id FROM showcase_asset_ids);
DELETE FROM test.asset
WHERE id IN (SELECT id FROM showcase_asset_ids);
DELETE FROM test.manufacturing_batch
WHERE batch_code LIKE 'SHOWCASE-%';
DELETE FROM test.station
WHERE station_code LIKE 'SHOWCASE-%';
DELETE FROM test.lab
WHERE lab_code LIKE 'SHOWCASE-%';
DELETE FROM test.site
WHERE site_code LIKE 'SHOWCASE-%';

DELETE FROM catalog.test_case_version
WHERE test_case_id IN (
    SELECT id FROM catalog.test_case WHERE case_code LIKE 'SHOWCASE-%'
);
DELETE FROM catalog.test_case
WHERE case_code LIKE 'SHOWCASE-%';
DELETE FROM catalog.health_check_policy
WHERE policy_code LIKE 'SHOWCASE-%';
DELETE FROM catalog.metric_version
WHERE metric_definition_id IN (
    SELECT id FROM catalog.metric_definition WHERE metric_code LIKE 'SHOWCASE-%'
);
DELETE FROM catalog.metric_definition
WHERE metric_code LIKE 'SHOWCASE-%';
DELETE FROM catalog.error_code
WHERE catalog_version_id IN (
    SELECT id FROM catalog.error_catalog_version WHERE version LIKE 'SHOWCASE-%'
);
DELETE FROM catalog.error_catalog_version
WHERE version LIKE 'SHOWCASE-%';

DELETE FROM integration.ingestion_receipt
WHERE source_id IN (
    SELECT id FROM integration.ingestion_source WHERE source_code LIKE 'SHOWCASE-%'
);
DELETE FROM integration.ingestion_source
WHERE source_code LIKE 'SHOWCASE-%';

DELETE FROM test.test_program
WHERE program_code LIKE 'SHOWCASE-%';

COMMIT;

\echo 'SHOWCASE records removed; immutable audit history was retained.'
