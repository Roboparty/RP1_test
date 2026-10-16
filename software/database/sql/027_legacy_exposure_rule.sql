-- A confirmed exposure assessment may include LEGACY_REPORTED time.
-- Unassessed historical intervals stay out of effective exposure.

CREATE OR REPLACE FUNCTION reliability.validate_exposure_assessment()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    v_campaign bigint;
    v_asset bigint;
    v_config_asset bigint;
BEGIN
    SELECT campaign_id, asset_id
    INTO v_campaign, v_asset
    FROM test.runtime_interval
    WHERE id = NEW.runtime_interval_id;

    IF v_campaign IS DISTINCT FROM NEW.campaign_id OR v_asset IS DISTINCT FROM NEW.asset_id THEN
        RAISE EXCEPTION 'exposure assessment does not match runtime interval campaign/asset';
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

CREATE OR REPLACE VIEW reliability.mtbf_eligible_runtime AS
SELECT
    ea.id AS assessment_id,
    ea.public_id AS assessment_public_id,
    ea.campaign_id,
    ea.asset_id,
    ea.configuration_snapshot_id,
    ri.id AS runtime_interval_id,
    ri.started_at,
    ri.ended_at,
    ri.active_seconds,
    esa.scope_id,
    esa.rule_id
FROM reliability.exposure_assessment ea
JOIN test.runtime_interval ri ON ri.id = ea.runtime_interval_id
JOIN reliability.exposure_scope_assignment esa
  ON esa.assessment_id = ea.id
WHERE ea.eligible
  AND ea.assignment_status = 'CONFIRMED'
  AND ea.quality_status IN ('VALID', 'PARTIAL')
  AND esa.inclusion_status = 'INCLUDED'
  AND NOT ri.voided;
