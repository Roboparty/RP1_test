-- Make current projections plan/evaluation scoped and make recompute enqueue
-- coalescing truly idempotent while an active job already exists.

ALTER TABLE reliability.current_mtbf_result
    DROP CONSTRAINT IF EXISTS current_mtbf_result_population_id_scope_id_key;

ALTER TABLE reliability.current_conclusion
    DROP CONSTRAINT IF EXISTS current_conclusion_population_id_scope_id_key;

CREATE OR REPLACE FUNCTION reliability.enqueue_evaluation_recompute(
    p_plan_id bigint,
    p_evaluation_id bigint,
    p_reason text
)
RETURNS reliability.recompute_job
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, reliability
AS $$
DECLARE
    v_job reliability.recompute_job;
    v_key text := 'evaluation:' || p_evaluation_id::text || ':recalculate';
    v_inserted boolean := false;
BEGIN
    INSERT INTO reliability.recompute_job (
        plan_id, evaluation_id, job_kind, job_key,
        resource_kind, resource_key, reason, payload
    ) VALUES (
        p_plan_id, p_evaluation_id, 'RECALCULATE', v_key,
        'reliability.mtbf_plan_evaluation', p_evaluation_id::text, p_reason,
        jsonb_build_object('plan_id', p_plan_id, 'evaluation_id', p_evaluation_id)
    )
    ON CONFLICT (job_key) WHERE job_key <> '' AND status IN ('PENDING', 'PROCESSING')
    DO NOTHING
    RETURNING * INTO v_job;

    IF v_job.id IS NOT NULL THEN
        v_inserted := true;
    ELSE
        SELECT * INTO v_job
        FROM reliability.recompute_job
        WHERE job_key = v_key
          AND status IN ('PENDING', 'PROCESSING')
        ORDER BY id DESC
        LIMIT 1;

        UPDATE reliability.recompute_job
        SET reason = p_reason,
            payload = payload || jsonb_build_object(
                'plan_id', p_plan_id, 'evaluation_id', p_evaluation_id
            ),
            requested_at = clock_timestamp(),
            updated_at = clock_timestamp()
        WHERE id = v_job.id
        RETURNING * INTO v_job;
    END IF;

    UPDATE reliability.current_mtbf_result
    SET freshness = 'STALE', stale_reason = p_reason,
        pending_job_count = CASE WHEN v_inserted THEN pending_job_count + 1 ELSE pending_job_count END,
        updated_at = clock_timestamp()
    WHERE evaluation_id = p_evaluation_id;

    UPDATE reliability.current_conclusion
    SET freshness = 'STALE', stale_reason = p_reason, updated_at = clock_timestamp()
    WHERE evaluation_id = p_evaluation_id;

    RETURN v_job;
END;
$$;

GRANT EXECUTE ON FUNCTION reliability.enqueue_evaluation_recompute(bigint, bigint, text)
TO __RP1_APP_USER__;
