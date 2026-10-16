-- Confirm existing LEGACY_REPORTED intervals as effective exposure.
-- Run only for a site that has explicitly accepted this historical time.
-- Requires iam.set_request_context already set in the same transaction.

DO $$
BEGIN
    IF iam.current_user_id() IS NULL THEN
        RAISE EXCEPTION 'request context is required';
    END IF;
    IF EXISTS (
        SELECT 1
        FROM test.runtime_interval
        WHERE source_kind = 'LEGACY_REPORTED'
          AND NOT voided
          AND data_quality NOT IN ('VALID', 'PARTIAL')
    ) THEN
        RAISE EXCEPTION 'legacy interval quality cannot be confirmed as eligible';
    END IF;
END;
$$;

INSERT INTO reliability.exposure_assessment(
    campaign_id, asset_id, runtime_interval_id, configuration_snapshot_id,
    eligible, assignment_status, quality_status, assessment_method,
    assessment_rule_version, exclusion_reason, assessed_by, assessed_at
)
SELECT
    ri.campaign_id,
    ri.asset_id,
    ri.id,
    e.configuration_snapshot_id,
    true,
    'CONFIRMED',
    ri.data_quality,
    'historical_record_confirmation',
    '20261012',
    '',
    iam.current_user_id(),
    clock_timestamp()
FROM test.runtime_interval ri
JOIN test.test_execution e ON e.id = ri.execution_id
WHERE ri.source_kind = 'LEGACY_REPORTED'
  AND NOT ri.voided
ON CONFLICT (runtime_interval_id) DO NOTHING;

INSERT INTO reliability.exposure_scope_assignment(
    campaign_id, assessment_id, scope_id, inclusion_status, rationale
)
SELECT
    ea.campaign_id,
    ea.id,
    scope.id,
    'INCLUDED',
    '用户确认将迁移的历史上报时长计入有效暴露'
FROM reliability.exposure_assessment ea
JOIN test.runtime_interval ri ON ri.id = ea.runtime_interval_id
JOIN test.asset a ON a.id = ea.asset_id
JOIN LATERAL (
    SELECT id
    FROM reliability.mtbf_scope
    WHERE scope_code = 'OVERALL'
      AND asset_kind = a.asset_kind
      AND status = 'PUBLISHED'
    ORDER BY version DESC
    LIMIT 1
) scope ON true
WHERE ri.source_kind = 'LEGACY_REPORTED'
  AND NOT ri.voided
  AND ea.assessment_method = 'historical_record_confirmation'
  AND ea.eligible
  AND ea.assignment_status = 'CONFIRMED'
ON CONFLICT (assessment_id, scope_id) DO NOTHING;

DO $$
DECLARE
    v_intervals bigint;
    v_included bigint;
BEGIN
    SELECT count(*) INTO v_intervals
    FROM test.runtime_interval
    WHERE source_kind = 'LEGACY_REPORTED' AND NOT voided;
    SELECT count(*) INTO v_included
    FROM test.runtime_interval ri
    JOIN reliability.exposure_assessment ea ON ea.runtime_interval_id = ri.id
    JOIN reliability.exposure_scope_assignment esa ON esa.assessment_id = ea.id
    JOIN test.asset a ON a.id = ri.asset_id
    JOIN reliability.mtbf_scope s ON s.id = esa.scope_id
    WHERE ri.source_kind = 'LEGACY_REPORTED'
      AND NOT ri.voided
      AND ea.eligible
      AND ea.assignment_status = 'CONFIRMED'
      AND ea.quality_status IN ('VALID', 'PARTIAL')
      AND esa.inclusion_status = 'INCLUDED'
      AND s.scope_code = 'OVERALL'
      AND s.status = 'PUBLISHED';
    IF v_intervals <> v_included THEN
        RAISE EXCEPTION 'legacy confirmation count mismatch'
            USING DETAIL = v_included::text || '/' || v_intervals::text;
    END IF;
END;
$$;
