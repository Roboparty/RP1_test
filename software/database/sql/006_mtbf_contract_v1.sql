-- RP1 MTBF backend rule contract V1.
-- This migration is additive: existing runtime, interruption and health facts
-- remain intact while formal MTBF decisions move to versioned ledgers.

ALTER TABLE iam.app_user
    ADD COLUMN IF NOT EXISTS principal_kind text NOT NULL DEFAULT 'HUMAN';

ALTER TABLE iam.app_user
    DROP CONSTRAINT IF EXISTS app_user_principal_kind_check;
ALTER TABLE iam.app_user
    ADD CONSTRAINT app_user_principal_kind_check
    CHECK (principal_kind IN ('HUMAN', 'SERVICE'));

CREATE OR REPLACE FUNCTION iam.current_principal_kind()
RETURNS text
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, iam
AS $$
    SELECT principal_kind
    FROM iam.app_user
    WHERE id = iam.current_user_id()
      AND enabled
$$;

CREATE OR REPLACE FUNCTION iam.is_system_admin()
RETURNS boolean
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, iam
AS $$
    SELECT coalesce(
        iam.current_user_role() = 'SYSTEM_ADMIN'
        AND iam.current_principal_kind() = 'HUMAN',
        false
    )
$$;

CREATE OR REPLACE FUNCTION iam.is_internal_service()
RETURNS boolean
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, iam
AS $$
    SELECT coalesce(iam.current_principal_kind() = 'SERVICE', false)
$$;

ALTER TABLE reliability.mtbf_population
    ADD COLUMN IF NOT EXISTS version integer NOT NULL DEFAULT 1,
    ADD COLUMN IF NOT EXISTS supersedes_population_id bigint REFERENCES reliability.mtbf_population(id),
    ADD COLUMN IF NOT EXISTS frozen_snapshot jsonb NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS content_hash text NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS frozen_at timestamptz;

CREATE TABLE reliability.mtbf_test_plan (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    public_id uuid NOT NULL DEFAULT public.uuid_v7() UNIQUE,
    plan_code text NOT NULL UNIQUE,
    name text NOT NULL,
    parent_plan_id bigint REFERENCES reliability.mtbf_test_plan(id),
    version integer NOT NULL DEFAULT 1 CHECK (version > 0),
    population_id bigint NOT NULL REFERENCES reliability.mtbf_population(id),
    method_id bigint NOT NULL REFERENCES reliability.statistics_method(id),
    execution_state text NOT NULL DEFAULT 'DRAFT' CHECK (
        execution_state IN (
            'DRAFT', 'PRETEST', 'READINESS_REVIEW', 'READY_FOR_FORMAL',
            'FORMAL_RUNNING', 'SUSPENDED', 'PENDING_REVIEW', 'CLOSED'
        )
    ),
    execution_state_version integer NOT NULL DEFAULT 1 CHECK (execution_state_version > 0),
    rule_hash text NOT NULL,
    frozen_parameters jsonb NOT NULL DEFAULT '{}'::jsonb,
    target_total_seconds numeric(24,3) NOT NULL DEFAULT 6696000 CHECK (target_total_seconds > 0),
    member_target_seconds numeric(24,3) NOT NULL DEFAULT 2232000 CHECK (member_target_seconds > 0),
    member_hard_min_seconds numeric(24,3) NOT NULL DEFAULT 1800000 CHECK (member_hard_min_seconds > 0),
    acceptance_failure_count integer NOT NULL DEFAULT 2 CHECK (acceptance_failure_count >= 0),
    theta0_hours numeric(18,6) NOT NULL DEFAULT 1000 CHECK (theta0_hours > 0),
    theta1_hours numeric(18,6) NOT NULL DEFAULT 500 CHECK (theta1_hours > 0),
    alpha numeric(20,18) NOT NULL DEFAULT 0.285493761136965952 CHECK (alpha > 0 AND alpha < 1),
    beta numeric(20,18) NOT NULL DEFAULT 0.282063998549568060 CHECK (beta > 0 AND beta < 1),
    data_cutoff_at timestamptz,
    formal_window_started_at timestamptz,
    formal_window_ended_at timestamptz,
    created_by bigint NOT NULL REFERENCES iam.app_user(id),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK (member_hard_min_seconds <= member_target_seconds),
    CHECK (theta0_hours >= theta1_hours),
    CHECK (formal_window_ended_at IS NULL OR formal_window_started_at IS NULL OR formal_window_ended_at >= formal_window_started_at)
);

CREATE TABLE reliability.mtbf_plan_evaluation (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    public_id uuid NOT NULL DEFAULT public.uuid_v7() UNIQUE,
    plan_id bigint NOT NULL REFERENCES reliability.mtbf_test_plan(id) ON DELETE CASCADE,
    scope_id bigint NOT NULL REFERENCES reliability.mtbf_scope(id),
    evaluation_kind text NOT NULL CHECK (evaluation_kind IN ('PRIMARY_DECISION', 'DERIVED')),
    name text NOT NULL,
    status text NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE', 'CLOSED')),
    rule_overrides jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_by bigint NOT NULL REFERENCES iam.app_user(id),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (plan_id, scope_id, evaluation_kind)
);

CREATE UNIQUE INDEX uq_mtbf_plan_primary_evaluation
    ON reliability.mtbf_plan_evaluation(plan_id)
    WHERE evaluation_kind = 'PRIMARY_DECISION';

CREATE TABLE reliability.mtbf_plan_campaign (
    plan_id bigint NOT NULL REFERENCES reliability.mtbf_test_plan(id) ON DELETE CASCADE,
    campaign_id bigint NOT NULL REFERENCES test.test_campaign(id),
    purpose text NOT NULL CHECK (purpose IN ('FORMAL_G2', 'RELATED_G1', 'SUPPLEMENTAL')),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (plan_id, campaign_id, purpose)
);

CREATE TABLE reliability.member_selection_revision (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    public_id uuid NOT NULL DEFAULT public.uuid_v7() UNIQUE,
    plan_id bigint NOT NULL REFERENCES reliability.mtbf_test_plan(id) ON DELETE CASCADE,
    revision_no integer NOT NULL CHECK (revision_no > 0),
    member_snapshot jsonb NOT NULL,
    content_hash text NOT NULL,
    status text NOT NULL CHECK (status IN ('PROPOSED', 'APPROVED', 'REJECTED')),
    proposed_by bigint NOT NULL REFERENCES iam.app_user(id),
    proposed_at timestamptz NOT NULL DEFAULT now(),
    approved_by bigint REFERENCES iam.app_user(id),
    approved_at timestamptz,
    reason text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (plan_id, revision_no),
    CHECK (approved_by IS NULL OR proposed_by <> approved_by),
    CHECK ((status = 'APPROVED') = (approved_by IS NOT NULL AND approved_at IS NOT NULL))
);

CREATE TABLE reliability.mtbf_plan_member (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    public_id uuid NOT NULL DEFAULT public.uuid_v7() UNIQUE,
    plan_id bigint NOT NULL REFERENCES reliability.mtbf_test_plan(id) ON DELETE CASCADE,
    asset_id bigint NOT NULL REFERENCES test.asset(id),
    member_role text NOT NULL CHECK (member_role IN ('STANDARD', 'PRO', 'FMEA_WORST_CASE')),
    selection_method text NOT NULL CHECK (
        selection_method IN ('RANDOM', 'SOLE_ELIGIBLE', 'APPROVED_ENGINEERING_SELECTION')
    ),
    configuration_snapshot_id bigint NOT NULL REFERENCES test.configuration_snapshot(id),
    selection_revision_id bigint NOT NULL REFERENCES reliability.member_selection_revision(id),
    target_seconds numeric(24,3) NOT NULL DEFAULT 2232000 CHECK (target_seconds > 0),
    hard_min_seconds numeric(24,3) NOT NULL DEFAULT 1800000 CHECK (hard_min_seconds > 0),
    zero_hour_health_run_id bigint REFERENCES health.health_check_run(id),
    membership_status text NOT NULL DEFAULT 'ACTIVE' CHECK (membership_status IN ('ACTIVE', 'COMPLETED', 'INVALIDATED')),
    joined_at timestamptz NOT NULL DEFAULT now(),
    left_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (plan_id, asset_id),
    UNIQUE (plan_id, member_role),
    CHECK (hard_min_seconds <= target_seconds),
    CHECK (left_at IS NULL OR left_at >= joined_at)
);

CREATE TABLE reliability.plan_control_revision (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    public_id uuid NOT NULL DEFAULT public.uuid_v7() UNIQUE,
    plan_id bigint NOT NULL REFERENCES reliability.mtbf_test_plan(id) ON DELETE CASCADE,
    revision_no integer NOT NULL CHECK (revision_no > 0),
    control_type text NOT NULL CHECK (
        control_type IN (
            'READINESS', 'HOMOGENEITY', 'WORST_CASE_SELECTION', 'TASK_PROFILE',
            'SCOPE_APPLICABILITY', 'PERFORMANCE_REQUIREMENT', 'DATA_INTEGRITY',
            'EVIDENCE_MANIFEST', 'CONFIGURATION_DISPOSITION'
        )
    ),
    payload jsonb NOT NULL,
    schema_version text NOT NULL,
    content_hash text NOT NULL,
    status text NOT NULL CHECK (status IN ('PROPOSED', 'APPROVED', 'REJECTED', 'SUPERSEDED')),
    effective_from timestamptz,
    effective_to timestamptz,
    proposed_by bigint NOT NULL REFERENCES iam.app_user(id),
    proposed_at timestamptz NOT NULL DEFAULT now(),
    approved_by bigint REFERENCES iam.app_user(id),
    approved_at timestamptz,
    reason text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (plan_id, control_type, revision_no),
    CHECK (approved_by IS NULL OR proposed_by <> approved_by),
    CHECK (effective_to IS NULL OR effective_from IS NULL OR effective_to > effective_from)
);

CREATE TABLE reliability.failure_case (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    public_id uuid NOT NULL DEFAULT public.uuid_v7() UNIQUE,
    plan_id bigint NOT NULL REFERENCES reliability.mtbf_test_plan(id) ON DELETE CASCADE,
    case_code text NOT NULL,
    title text NOT NULL,
    summary text NOT NULL DEFAULT '',
    case_status text NOT NULL DEFAULT 'OPEN' CHECK (case_status IN ('OPEN', 'UNDER_REVIEW', 'RESOLVED', 'CLOSED')),
    root_cause_key text NOT NULL DEFAULT '',
    opened_by bigint NOT NULL REFERENCES iam.app_user(id),
    opened_at timestamptz NOT NULL DEFAULT now(),
    closed_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (plan_id, case_code),
    CHECK (closed_at IS NULL OR closed_at >= opened_at)
);

CREATE TABLE reliability.failure_case_event (
    failure_case_id bigint NOT NULL REFERENCES reliability.failure_case(id) ON DELETE CASCADE,
    event_id bigint NOT NULL REFERENCES test.test_event(id),
    event_role text NOT NULL CHECK (event_role IN ('PRIMARY', 'SYMPTOM', 'RECURRENCE_EVIDENCE')),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (failure_case_id, event_id)
);

CREATE TABLE reliability.failure_case_relation (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_case_id bigint NOT NULL REFERENCES reliability.failure_case(id),
    target_case_id bigint NOT NULL REFERENCES reliability.failure_case(id),
    relation_type text NOT NULL CHECK (relation_type IN ('MERGED_INTO', 'SPLIT_FROM', 'RECURRENCE_OF', 'DUPLICATE_OF')),
    rationale text NOT NULL,
    created_by bigint NOT NULL REFERENCES iam.app_user(id),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (source_case_id, target_case_id, relation_type),
    CHECK (source_case_id <> target_case_id)
);

CREATE TABLE reliability.failure_adjudication_revision (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    public_id uuid NOT NULL DEFAULT public.uuid_v7() UNIQUE,
    failure_case_id bigint NOT NULL REFERENCES reliability.failure_case(id) ON DELETE CASCADE,
    evaluation_id bigint NOT NULL REFERENCES reliability.mtbf_plan_evaluation(id) ON DELETE CASCADE,
    revision_no integer NOT NULL CHECK (revision_no > 0),
    relevance text NOT NULL CHECK (relevance IN ('RELEVANT', 'NON_RELEVANT', 'PENDING')),
    independence text NOT NULL CHECK (independence IN ('COUNTED_ROOT', 'SAME_UNRESOLVED_ROOT', 'DUPLICATE', 'PENDING')),
    contribution smallint NOT NULL CHECK (contribution IN (0, 1)),
    failure_boundary_at timestamptz,
    rationale text NOT NULL,
    evidence_refs jsonb NOT NULL DEFAULT '[]'::jsonb,
    status text NOT NULL CHECK (status IN ('PROPOSED', 'APPROVED', 'REJECTED', 'SUPERSEDED')),
    proposed_by bigint NOT NULL REFERENCES iam.app_user(id),
    proposed_at timestamptz NOT NULL DEFAULT now(),
    approved_by bigint REFERENCES iam.app_user(id),
    approved_at timestamptz,
    effective_from timestamptz,
    knowledge_recorded_at timestamptz NOT NULL DEFAULT now(),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (failure_case_id, evaluation_id, revision_no),
    CHECK (approved_by IS NULL OR proposed_by <> approved_by),
    CHECK (contribution = 0 OR (relevance = 'RELEVANT' AND independence = 'COUNTED_ROOT'))
);

CREATE TABLE reliability.exposure_slice (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    public_id uuid NOT NULL DEFAULT public.uuid_v7() UNIQUE,
    plan_id bigint NOT NULL REFERENCES reliability.mtbf_test_plan(id) ON DELETE CASCADE,
    evaluation_id bigint NOT NULL REFERENCES reliability.mtbf_plan_evaluation(id) ON DELETE CASCADE,
    member_id bigint NOT NULL REFERENCES reliability.mtbf_plan_member(id),
    runtime_interval_id bigint REFERENCES test.runtime_interval(id),
    slice_origin text NOT NULL DEFAULT 'RUNTIME_INTERVAL' CHECK (slice_origin IN ('RUNTIME_INTERVAL', 'DERIVED_CHECKPOINT')),
    start_at timestamptz NOT NULL,
    end_at timestamptz NOT NULL,
    time_range tstzrange GENERATED ALWAYS AS (tstzrange(start_at, end_at, '[)')) STORED,
    evidence_hash text NOT NULL,
    created_by bigint NOT NULL REFERENCES iam.app_user(id),
    created_at timestamptz NOT NULL DEFAULT now(),
    CHECK (end_at > start_at),
    CHECK (slice_origin <> 'RUNTIME_INTERVAL' OR runtime_interval_id IS NOT NULL)
);

CREATE TABLE reliability.exposure_disposition_revision (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    public_id uuid NOT NULL DEFAULT public.uuid_v7() UNIQUE,
    exposure_slice_id bigint NOT NULL REFERENCES reliability.exposure_slice(id) ON DELETE CASCADE,
    revision_no integer NOT NULL CHECK (revision_no > 0),
    disposition text NOT NULL CHECK (disposition IN ('INCLUDED', 'EXCLUDED', 'PENDING')),
    reason_code text NOT NULL,
    rationale text NOT NULL,
    evidence_refs jsonb NOT NULL DEFAULT '[]'::jsonb,
    status text NOT NULL CHECK (status IN ('PROPOSED', 'APPROVED', 'REJECTED', 'SUPERSEDED')),
    proposed_by bigint NOT NULL REFERENCES iam.app_user(id),
    proposed_at timestamptz NOT NULL DEFAULT now(),
    approved_by bigint REFERENCES iam.app_user(id),
    approved_at timestamptz,
    effective_from timestamptz,
    knowledge_recorded_at timestamptz NOT NULL DEFAULT now(),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (exposure_slice_id, revision_no),
    CHECK (approved_by IS NULL OR proposed_by <> approved_by)
);

ALTER TABLE reliability.calculation_run
    ADD COLUMN IF NOT EXISTS plan_id bigint REFERENCES reliability.mtbf_test_plan(id),
    ADD COLUMN IF NOT EXISTS evaluation_id bigint REFERENCES reliability.mtbf_plan_evaluation(id),
    ADD COLUMN IF NOT EXISTS knowledge_as_of_at timestamptz NOT NULL DEFAULT now(),
    ADD COLUMN IF NOT EXISTS dependency_hash text NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS implementation_version text NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS formal_exposure_seconds numeric(24,3) NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS provisional_exposure_seconds numeric(24,3) NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS pending_exposure_seconds numeric(24,3) NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS excluded_exposure_seconds numeric(24,3) NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS relevant_failure_count integer NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS pending_failure_case_count integer NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS point_estimate_hours numeric(24,9),
    ADD COLUMN IF NOT EXISTS lower_70_hours numeric(24,9),
    ADD COLUMN IF NOT EXISTS lower_90_hours numeric(24,9),
    ADD COLUMN IF NOT EXISTS point_estimate_status text NOT NULL DEFAULT 'NOT_CALCULATED';

ALTER TABLE reliability.calculation_run
    DROP CONSTRAINT IF EXISTS calculation_run_status_check;
ALTER TABLE reliability.calculation_run
    ADD CONSTRAINT calculation_run_status_check CHECK (
        status IN ('QUEUED', 'RUNNING', 'SUCCEEDED', 'FAILED', 'SUPERSEDED_DURING_RUN')
    );
ALTER TABLE reliability.calculation_run
    ADD CONSTRAINT calculation_run_v1_counts_check CHECK (
        formal_exposure_seconds >= 0
        AND provisional_exposure_seconds >= 0
        AND pending_exposure_seconds >= 0
        AND excluded_exposure_seconds >= 0
        AND relevant_failure_count >= 0
        AND pending_failure_case_count >= 0
    );

CREATE TABLE reliability.calculation_member_result (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    calculation_run_id bigint NOT NULL REFERENCES reliability.calculation_run(id) ON DELETE CASCADE,
    member_id bigint NOT NULL REFERENCES reliability.mtbf_plan_member(id),
    formal_seconds numeric(24,3) NOT NULL CHECK (formal_seconds >= 0),
    provisional_seconds numeric(24,3) NOT NULL CHECK (provisional_seconds >= 0),
    pending_seconds numeric(24,3) NOT NULL CHECK (pending_seconds >= 0),
    excluded_seconds numeric(24,3) NOT NULL CHECK (excluded_seconds >= 0),
    reached_target boolean NOT NULL,
    reached_hard_minimum boolean NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (calculation_run_id, member_id)
);

CREATE TABLE reliability.calculation_input_ref (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    calculation_run_id bigint NOT NULL REFERENCES reliability.calculation_run(id) ON DELETE CASCADE,
    input_type text NOT NULL CHECK (input_type IN ('EXPOSURE_SLICE', 'FAILURE_CASE', 'REVISION', 'MANIFEST', 'RULE')),
    entity_public_id uuid,
    entity_key text NOT NULL DEFAULT '',
    content_hash text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (calculation_run_id, input_type, entity_key, content_hash)
);

CREATE TABLE reliability.plan_gate_evaluation_revision (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    public_id uuid NOT NULL DEFAULT public.uuid_v7() UNIQUE,
    plan_id bigint NOT NULL REFERENCES reliability.mtbf_test_plan(id) ON DELETE CASCADE,
    evaluation_id bigint NOT NULL REFERENCES reliability.mtbf_plan_evaluation(id) ON DELETE CASCADE,
    calculation_run_id bigint REFERENCES reliability.calculation_run(id),
    gate_type text NOT NULL CHECK (gate_type IN ('SAFETY', 'STATISTICAL', 'FUNCTIONAL_PERFORMANCE', 'DATA_INTEGRITY')),
    revision_no integer NOT NULL CHECK (revision_no > 0),
    machine_status text NOT NULL CHECK (machine_status IN ('PASS', 'FAIL', 'PENDING', 'NOT_APPLICABLE')),
    review_status text NOT NULL CHECK (review_status IN ('PENDING', 'PASS', 'FAIL')),
    evidence_refs jsonb NOT NULL DEFAULT '[]'::jsonb,
    dependency_hash text NOT NULL,
    proposed_by bigint NOT NULL REFERENCES iam.app_user(id),
    proposed_at timestamptz NOT NULL DEFAULT now(),
    reviewed_by bigint REFERENCES iam.app_user(id),
    reviewed_at timestamptz,
    rationale text NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (evaluation_id, gate_type, revision_no),
    CHECK (reviewed_by IS NULL OR proposed_by <> reviewed_by)
);

CREATE TABLE reliability.formal_conclusion_revision (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    public_id uuid NOT NULL DEFAULT public.uuid_v7() UNIQUE,
    plan_id bigint NOT NULL REFERENCES reliability.mtbf_test_plan(id) ON DELETE CASCADE,
    evaluation_id bigint NOT NULL REFERENCES reliability.mtbf_plan_evaluation(id),
    calculation_run_id bigint NOT NULL REFERENCES reliability.calculation_run(id),
    revision_no integer NOT NULL CHECK (revision_no > 0),
    relationship text NOT NULL DEFAULT 'INITIAL' CHECK (relationship IN ('INITIAL', 'REVALIDATES', 'SUPERSEDES')),
    supersedes_revision_id bigint REFERENCES reliability.formal_conclusion_revision(id),
    decision_outcome text NOT NULL CHECK (decision_outcome IN ('ACCEPTED', 'REJECTED', 'NO_FORMAL_DECISION')),
    disposition_reason text NOT NULL,
    publication_validity text NOT NULL CHECK (
        publication_validity IN ('DRAFT', 'PENDING_SIGNATURE', 'PUBLISHED_CURRENT', 'STALE_UNDER_REVIEW', 'SUPERSEDED', 'WITHDRAWN')
    ),
    snapshot_payload jsonb NOT NULL,
    dependency_hash text NOT NULL,
    content_hash text NOT NULL,
    proposed_by bigint NOT NULL REFERENCES iam.app_user(id),
    proposed_at timestamptz NOT NULL DEFAULT now(),
    published_by bigint REFERENCES iam.app_user(id),
    published_at timestamptz,
    reason text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (plan_id, revision_no),
    CHECK (published_by IS NULL OR proposed_by <> published_by),
    CHECK ((publication_validity = 'PUBLISHED_CURRENT') = (published_by IS NOT NULL AND published_at IS NOT NULL))
);

CREATE TABLE reliability.plan_state_transition (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    public_id uuid NOT NULL DEFAULT public.uuid_v7() UNIQUE,
    plan_id bigint NOT NULL REFERENCES reliability.mtbf_test_plan(id) ON DELETE CASCADE,
    from_state text NOT NULL,
    to_state text NOT NULL,
    expected_state_version integer NOT NULL,
    transition_reason text NOT NULL,
    dependency_hash text NOT NULL,
    requested_by bigint NOT NULL REFERENCES iam.app_user(id),
    approved_by bigint REFERENCES iam.app_user(id),
    occurred_at timestamptz NOT NULL DEFAULT now(),
    CHECK (from_state <> to_state),
    CHECK (approved_by IS NULL OR requested_by <> approved_by)
);

CREATE TABLE reliability.plan_hold_revision (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    public_id uuid NOT NULL DEFAULT public.uuid_v7() UNIQUE,
    plan_id bigint NOT NULL REFERENCES reliability.mtbf_test_plan(id) ON DELETE CASCADE,
    evaluation_id bigint REFERENCES reliability.mtbf_plan_evaluation(id),
    hold_key text NOT NULL,
    revision_no integer NOT NULL CHECK (revision_no > 0),
    hold_type text NOT NULL CHECK (
        hold_type IN (
            'SAFETY_HOLD', 'READINESS_LOST', 'HOMOGENEITY_PENDING',
            'DATA_INTEGRITY_BLOCK', 'EARLY_REJECT_CANDIDATE', 'DECISION_STALE',
            'RECOMPUTE_PENDING', 'CONFIG_REVIEW_PENDING'
        )
    ),
    severity text NOT NULL CHECK (severity IN ('BLOCKING', 'ADVISORY')),
    action text NOT NULL CHECK (action IN ('OPEN', 'CLOSE')),
    trigger_evidence jsonb NOT NULL DEFAULT '[]'::jsonb,
    recovery_conditions jsonb NOT NULL DEFAULT '[]'::jsonb,
    proposed_by bigint NOT NULL REFERENCES iam.app_user(id),
    proposed_at timestamptz NOT NULL DEFAULT now(),
    approved_by bigint REFERENCES iam.app_user(id),
    approved_at timestamptz,
    reason text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (plan_id, hold_key, revision_no),
    CHECK (approved_by IS NULL OR proposed_by <> approved_by)
);

CREATE TABLE reliability.plan_capability_assignment (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    public_id uuid NOT NULL DEFAULT public.uuid_v7() UNIQUE,
    plan_id bigint NOT NULL REFERENCES reliability.mtbf_test_plan(id) ON DELETE CASCADE,
    evaluation_id bigint REFERENCES reliability.mtbf_plan_evaluation(id),
    campaign_id bigint REFERENCES test.test_campaign(id),
    user_id bigint NOT NULL REFERENCES iam.app_user(id),
    capability text NOT NULL CHECK (
        capability IN (
            'EXECUTE_TEST', 'EDIT_FACT', 'PROPOSE_ADJUDICATION', 'REVIEW_FAILURE',
            'REVIEW_CONFIG', 'REVIEW_DATA', 'REVIEW_GATE', 'SAFETY_REVIEW',
            'PUBLISH_CONCLUSION'
        )
    ),
    granted_by bigint NOT NULL REFERENCES iam.app_user(id),
    granted_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz,
    revoked_by bigint REFERENCES iam.app_user(id),
    revoked_at timestamptz,
    reason text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    CHECK (expires_at IS NULL OR expires_at > granted_at),
    CHECK ((revoked_by IS NULL) = (revoked_at IS NULL))
);

CREATE UNIQUE INDEX uq_active_plan_capability
    ON reliability.plan_capability_assignment(plan_id, coalesce(evaluation_id, 0), coalesce(campaign_id, 0), user_id, capability)
    WHERE revoked_at IS NULL;

ALTER TABLE reliability.current_mtbf_result
    ADD COLUMN IF NOT EXISTS plan_id bigint REFERENCES reliability.mtbf_test_plan(id),
    ADD COLUMN IF NOT EXISTS evaluation_id bigint REFERENCES reliability.mtbf_plan_evaluation(id),
    ADD COLUMN IF NOT EXISTS freshness text NOT NULL DEFAULT 'STALE' CHECK (freshness IN ('CURRENT', 'STALE', 'RECOMPUTING')),
    ADD COLUMN IF NOT EXISTS stale_reason text NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS pending_job_count integer NOT NULL DEFAULT 0 CHECK (pending_job_count >= 0),
    ADD COLUMN IF NOT EXISTS provisional_exposure_seconds numeric(24,3) NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS excluded_exposure_seconds numeric(24,3) NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS lower_70_hours numeric(24,9),
    ADD COLUMN IF NOT EXISTS lower_90_hours numeric(24,9);

CREATE UNIQUE INDEX uq_current_mtbf_result_evaluation
    ON reliability.current_mtbf_result(evaluation_id)
    WHERE evaluation_id IS NOT NULL;

ALTER TABLE reliability.current_conclusion
    ADD COLUMN IF NOT EXISTS plan_id bigint REFERENCES reliability.mtbf_test_plan(id),
    ADD COLUMN IF NOT EXISTS evaluation_id bigint REFERENCES reliability.mtbf_plan_evaluation(id),
    ADD COLUMN IF NOT EXISTS conclusion_revision_id bigint REFERENCES reliability.formal_conclusion_revision(id),
    ADD COLUMN IF NOT EXISTS publication_validity text NOT NULL DEFAULT 'DRAFT',
    ADD COLUMN IF NOT EXISTS freshness text NOT NULL DEFAULT 'STALE' CHECK (freshness IN ('CURRENT', 'STALE')),
    ADD COLUMN IF NOT EXISTS stale_reason text NOT NULL DEFAULT '';

CREATE UNIQUE INDEX uq_current_conclusion_evaluation
    ON reliability.current_conclusion(evaluation_id)
    WHERE evaluation_id IS NOT NULL;

ALTER TABLE reliability.recompute_job
    ADD COLUMN IF NOT EXISTS plan_id bigint REFERENCES reliability.mtbf_test_plan(id),
    ADD COLUMN IF NOT EXISTS evaluation_id bigint REFERENCES reliability.mtbf_plan_evaluation(id),
    ADD COLUMN IF NOT EXISTS job_kind text NOT NULL DEFAULT 'RECALCULATE',
    ADD COLUMN IF NOT EXISTS job_key text NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS dependency_hash text NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS priority integer NOT NULL DEFAULT 100,
    ADD COLUMN IF NOT EXISTS max_attempts integer NOT NULL DEFAULT 5 CHECK (max_attempts > 0);

ALTER TABLE reliability.recompute_job
    DROP CONSTRAINT IF EXISTS recompute_job_status_check;
ALTER TABLE reliability.recompute_job
    ADD CONSTRAINT recompute_job_status_check CHECK (
        status IN ('PENDING', 'PROCESSING', 'SUCCEEDED', 'FAILED', 'DEAD_LETTER')
    );

CREATE UNIQUE INDEX uq_recompute_active_job_key
    ON reliability.recompute_job(job_key)
    WHERE job_key <> '' AND status IN ('PENDING', 'PROCESSING');

-- Query indexes follow equality columns before time/range columns.
CREATE INDEX idx_mtbf_plan_population_state ON reliability.mtbf_test_plan(population_id, execution_state, updated_at DESC);
CREATE INDEX idx_mtbf_evaluation_plan ON reliability.mtbf_plan_evaluation(plan_id, evaluation_kind, status);
CREATE INDEX idx_mtbf_plan_campaign_campaign ON reliability.mtbf_plan_campaign(campaign_id, plan_id);
CREATE INDEX idx_mtbf_member_asset ON reliability.mtbf_plan_member(asset_id, plan_id);
CREATE INDEX idx_failure_case_plan_status ON reliability.failure_case(plan_id, case_status, opened_at DESC);
CREATE INDEX idx_failure_adjudication_current ON reliability.failure_adjudication_revision(evaluation_id, failure_case_id, revision_no DESC);
CREATE INDEX idx_exposure_disposition_current ON reliability.exposure_disposition_revision(exposure_slice_id, revision_no DESC);
CREATE INDEX idx_gate_current ON reliability.plan_gate_evaluation_revision(evaluation_id, gate_type, revision_no DESC);
CREATE INDEX idx_conclusion_plan_revision ON reliability.formal_conclusion_revision(plan_id, revision_no DESC);
CREATE INDEX idx_hold_plan_key_revision ON reliability.plan_hold_revision(plan_id, hold_key, revision_no DESC);
CREATE INDEX idx_capability_user_plan ON reliability.plan_capability_assignment(user_id, plan_id, capability) WHERE revoked_at IS NULL;
CREATE INDEX idx_recompute_v1_claim ON reliability.recompute_job(status, priority, available_at, id) WHERE status = 'PENDING';
CREATE INDEX idx_exposure_slice_range ON reliability.exposure_slice USING gist(evaluation_id, member_id, time_range);

CREATE OR REPLACE VIEW reliability.current_exposure_disposition
WITH (security_invoker = true) AS
SELECT DISTINCT ON (r.exposure_slice_id)
    r.*
FROM reliability.exposure_disposition_revision r
WHERE r.status = 'APPROVED'
ORDER BY r.exposure_slice_id, r.revision_no DESC;

CREATE OR REPLACE VIEW reliability.current_failure_adjudication
WITH (security_invoker = true) AS
SELECT DISTINCT ON (r.failure_case_id, r.evaluation_id)
    r.*
FROM reliability.failure_adjudication_revision r
WHERE r.status = 'APPROVED'
ORDER BY r.failure_case_id, r.evaluation_id, r.revision_no DESC;

CREATE OR REPLACE VIEW reliability.current_plan_hold
WITH (security_invoker = true) AS
SELECT DISTINCT ON (r.plan_id, r.hold_key)
    r.*
FROM reliability.plan_hold_revision r
ORDER BY r.plan_id, r.hold_key, r.revision_no DESC;

CREATE OR REPLACE FUNCTION iam.has_plan_capability(p_plan_id bigint, p_capability text)
RETURNS boolean
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, iam, reliability
AS $$
    SELECT iam.is_system_admin() OR EXISTS (
        SELECT 1
        FROM reliability.plan_capability_assignment pca
        WHERE pca.plan_id = p_plan_id
          AND pca.user_id = iam.current_user_id()
          AND pca.capability = p_capability
          AND pca.revoked_at IS NULL
          AND (pca.expires_at IS NULL OR pca.expires_at > clock_timestamp())
    )
$$;

CREATE OR REPLACE FUNCTION iam.can_access_mtbf_plan(p_plan_id bigint, p_require_edit boolean DEFAULT false)
RETURNS boolean
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, iam, reliability
AS $$
    SELECT CASE
        WHEN iam.is_internal_service() THEN true
        WHEN iam.is_system_admin() THEN true
        WHEN EXISTS (
            SELECT 1 FROM reliability.mtbf_test_plan p
            WHERE p.id = p_plan_id AND p.created_by = iam.current_user_id()
        ) THEN true
        WHEN p_require_edit AND iam.current_user_role() <> 'TEST_EXECUTOR' THEN false
        ELSE EXISTS (
            SELECT 1
            FROM reliability.mtbf_plan_campaign pc
            WHERE pc.plan_id = p_plan_id
              AND iam.can_access_campaign(pc.campaign_id, p_require_edit)
        )
    END
$$;

CREATE OR REPLACE FUNCTION iam.can_access_population(p_population_id bigint, p_require_edit boolean DEFAULT false)
RETURNS boolean
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, iam, reliability
AS $$
    SELECT iam.is_internal_service() OR iam.is_system_admin() OR EXISTS (
        SELECT 1
        FROM reliability.population_campaign pc
        WHERE pc.population_id = p_population_id
          AND iam.can_access_campaign(pc.campaign_id, p_require_edit)
    )
$$;

CREATE OR REPLACE FUNCTION reliability.reject_append_only_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION '% is append-only; create a new revision instead', TG_TABLE_SCHEMA || '.' || TG_TABLE_NAME;
END;
$$;

CREATE OR REPLACE FUNCTION reliability.validate_human_approver()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    v_approver bigint;
    v_kind text;
BEGIN
    v_approver := CASE
        WHEN TG_TABLE_NAME = 'plan_gate_evaluation_revision' THEN NEW.reviewed_by
        WHEN TG_TABLE_NAME = 'formal_conclusion_revision' THEN NEW.published_by
        WHEN TG_TABLE_NAME = 'plan_state_transition' THEN NEW.approved_by
        ELSE NEW.approved_by
    END;
    IF v_approver IS NULL THEN
        RETURN NEW;
    END IF;
    SELECT principal_kind INTO v_kind FROM iam.app_user WHERE id = v_approver AND enabled;
    IF v_kind IS DISTINCT FROM 'HUMAN' THEN
        RAISE EXCEPTION 'approval, review and publication require an enabled human principal';
    END IF;
    RETURN NEW;
END;
$$;

DO $$
DECLARE
    v_table text;
BEGIN
    FOREACH v_table IN ARRAY ARRAY[
        'reliability.member_selection_revision',
        'reliability.plan_control_revision',
        'reliability.failure_adjudication_revision',
        'reliability.exposure_disposition_revision',
        'reliability.plan_gate_evaluation_revision',
        'reliability.formal_conclusion_revision',
        'reliability.plan_state_transition',
        'reliability.plan_hold_revision',
        'reliability.calculation_member_result',
        'reliability.calculation_input_ref'
    ] LOOP
        EXECUTE format(
            'CREATE TRIGGER trg_append_only BEFORE UPDATE OR DELETE ON %s '
            'FOR EACH ROW EXECUTE FUNCTION reliability.reject_append_only_mutation()',
            v_table
        );
    END LOOP;
END;
$$;

DO $$
DECLARE
    v_table text;
BEGIN
    FOREACH v_table IN ARRAY ARRAY[
        'reliability.member_selection_revision',
        'reliability.plan_control_revision',
        'reliability.failure_adjudication_revision',
        'reliability.exposure_disposition_revision',
        'reliability.plan_gate_evaluation_revision',
        'reliability.formal_conclusion_revision',
        'reliability.plan_state_transition',
        'reliability.plan_hold_revision'
    ] LOOP
        EXECUTE format(
            'CREATE TRIGGER trg_human_approver BEFORE INSERT ON %s '
            'FOR EACH ROW EXECUTE FUNCTION reliability.validate_human_approver()',
            v_table
        );
    END LOOP;
END;
$$;

CREATE OR REPLACE FUNCTION reliability.claim_recompute_job(p_worker_id text)
RETURNS SETOF reliability.recompute_job
LANGUAGE sql
SECURITY DEFINER
SET search_path = pg_catalog, reliability
AS $$
    UPDATE reliability.recompute_job
    SET status = 'PROCESSING',
        attempts = attempts + 1,
        locked_by = p_worker_id,
        locked_at = clock_timestamp(),
        updated_at = clock_timestamp()
    WHERE id = (
        SELECT id
        FROM reliability.recompute_job
        WHERE status = 'PENDING'
          AND available_at <= clock_timestamp()
        ORDER BY priority, available_at, id
        FOR UPDATE SKIP LOCKED
        LIMIT 1
    )
    RETURNING *
$$;

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
BEGIN
    UPDATE reliability.current_mtbf_result
    SET freshness = 'STALE', stale_reason = p_reason, pending_job_count = pending_job_count + 1,
        updated_at = clock_timestamp()
    WHERE evaluation_id = p_evaluation_id;

    UPDATE reliability.current_conclusion
    SET freshness = 'STALE', stale_reason = p_reason, updated_at = clock_timestamp()
    WHERE evaluation_id = p_evaluation_id;

    INSERT INTO reliability.recompute_job (
        plan_id, evaluation_id, job_kind, job_key,
        resource_kind, resource_key, reason, payload
    ) VALUES (
        p_plan_id, p_evaluation_id, 'RECALCULATE', v_key,
        'reliability.mtbf_plan_evaluation', p_evaluation_id::text, p_reason,
        jsonb_build_object('plan_id', p_plan_id, 'evaluation_id', p_evaluation_id)
    )
    ON CONFLICT (job_key) WHERE job_key <> '' AND status IN ('PENDING', 'PROCESSING')
    DO UPDATE SET reason = EXCLUDED.reason,
                  payload = reliability.recompute_job.payload || EXCLUDED.payload,
                  requested_at = clock_timestamp(),
                  updated_at = clock_timestamp()
    RETURNING * INTO v_job;
    RETURN v_job;
END;
$$;

-- RLS for all V1 contract objects.
DO $$
DECLARE
    v_table text;
BEGIN
    FOREACH v_table IN ARRAY ARRAY[
        'reliability.mtbf_test_plan', 'reliability.mtbf_plan_evaluation',
        'reliability.mtbf_plan_campaign', 'reliability.member_selection_revision',
        'reliability.mtbf_plan_member', 'reliability.plan_control_revision',
        'reliability.failure_case', 'reliability.failure_case_event',
        'reliability.failure_case_relation', 'reliability.failure_adjudication_revision',
        'reliability.exposure_slice', 'reliability.exposure_disposition_revision',
        'reliability.calculation_member_result', 'reliability.calculation_input_ref',
        'reliability.plan_gate_evaluation_revision', 'reliability.formal_conclusion_revision',
        'reliability.plan_state_transition', 'reliability.plan_hold_revision',
        'reliability.plan_capability_assignment'
    ] LOOP
        EXECUTE format('ALTER TABLE %s ENABLE ROW LEVEL SECURITY', v_table);
    END LOOP;
END;
$$;

CREATE POLICY mtbf_plan_select ON reliability.mtbf_test_plan
    FOR SELECT USING (iam.can_access_mtbf_plan(id, false));
CREATE POLICY mtbf_plan_insert ON reliability.mtbf_test_plan
    FOR INSERT WITH CHECK (iam.can_write_business() AND created_by = iam.current_user_id());
CREATE POLICY mtbf_plan_update ON reliability.mtbf_test_plan
    FOR UPDATE USING (iam.can_access_mtbf_plan(id, true))
    WITH CHECK (iam.can_access_mtbf_plan(id, true));

DO $$
DECLARE
    v_table text;
    v_plan_expr text;
BEGIN
    FOR v_table, v_plan_expr IN
        SELECT * FROM (VALUES
            ('reliability.mtbf_plan_evaluation', 'plan_id'),
            ('reliability.mtbf_plan_campaign', 'plan_id'),
            ('reliability.member_selection_revision', 'plan_id'),
            ('reliability.mtbf_plan_member', 'plan_id'),
            ('reliability.plan_control_revision', 'plan_id'),
            ('reliability.failure_case', 'plan_id'),
            ('reliability.exposure_slice', 'plan_id'),
            ('reliability.plan_gate_evaluation_revision', 'plan_id'),
            ('reliability.formal_conclusion_revision', 'plan_id'),
            ('reliability.plan_state_transition', 'plan_id'),
            ('reliability.plan_hold_revision', 'plan_id')
        ) AS t(table_name, plan_expr)
    LOOP
        EXECUTE format(
            'CREATE POLICY plan_scoped_select ON %s FOR SELECT USING (iam.can_access_mtbf_plan(%s, false))',
            v_table, v_plan_expr
        );
        EXECUTE format(
            'CREATE POLICY plan_scoped_write ON %s FOR INSERT WITH CHECK (iam.can_access_mtbf_plan(%s, true))',
            v_table, v_plan_expr
        );
    END LOOP;
END;
$$;

CREATE POLICY failure_event_select ON reliability.failure_case_event
    FOR SELECT USING (EXISTS (SELECT 1 FROM reliability.failure_case c WHERE c.id = failure_case_id AND iam.can_access_mtbf_plan(c.plan_id, false)));
CREATE POLICY failure_event_insert ON reliability.failure_case_event
    FOR INSERT WITH CHECK (EXISTS (SELECT 1 FROM reliability.failure_case c WHERE c.id = failure_case_id AND iam.can_access_mtbf_plan(c.plan_id, true)));

CREATE POLICY failure_relation_select ON reliability.failure_case_relation
    FOR SELECT USING (EXISTS (SELECT 1 FROM reliability.failure_case c WHERE c.id = source_case_id AND iam.can_access_mtbf_plan(c.plan_id, false)));
CREATE POLICY failure_relation_insert ON reliability.failure_case_relation
    FOR INSERT WITH CHECK (EXISTS (SELECT 1 FROM reliability.failure_case c WHERE c.id = source_case_id AND iam.can_access_mtbf_plan(c.plan_id, true)));

CREATE POLICY adjudication_select ON reliability.failure_adjudication_revision
    FOR SELECT USING (EXISTS (SELECT 1 FROM reliability.mtbf_plan_evaluation e WHERE e.id = evaluation_id AND iam.can_access_mtbf_plan(e.plan_id, false)));
CREATE POLICY adjudication_insert ON reliability.failure_adjudication_revision
    FOR INSERT WITH CHECK (EXISTS (SELECT 1 FROM reliability.mtbf_plan_evaluation e WHERE e.id = evaluation_id AND iam.can_access_mtbf_plan(e.plan_id, true)));

CREATE POLICY exposure_revision_select ON reliability.exposure_disposition_revision
    FOR SELECT USING (EXISTS (SELECT 1 FROM reliability.exposure_slice s WHERE s.id = exposure_slice_id AND iam.can_access_mtbf_plan(s.plan_id, false)));
CREATE POLICY exposure_revision_insert ON reliability.exposure_disposition_revision
    FOR INSERT WITH CHECK (EXISTS (SELECT 1 FROM reliability.exposure_slice s WHERE s.id = exposure_slice_id AND iam.can_access_mtbf_plan(s.plan_id, true)));

CREATE POLICY calculation_member_select ON reliability.calculation_member_result
    FOR SELECT USING (EXISTS (SELECT 1 FROM reliability.calculation_run r WHERE r.id = calculation_run_id AND (iam.is_internal_service() OR iam.can_access_mtbf_plan(r.plan_id, false))));
CREATE POLICY calculation_member_insert ON reliability.calculation_member_result
    FOR INSERT WITH CHECK (iam.is_internal_service() OR EXISTS (SELECT 1 FROM reliability.calculation_run r WHERE r.id = calculation_run_id AND iam.can_access_mtbf_plan(r.plan_id, true)));

CREATE POLICY calculation_input_select ON reliability.calculation_input_ref
    FOR SELECT USING (EXISTS (SELECT 1 FROM reliability.calculation_run r WHERE r.id = calculation_run_id AND (iam.is_internal_service() OR iam.can_access_mtbf_plan(r.plan_id, false))));
CREATE POLICY calculation_input_insert ON reliability.calculation_input_ref
    FOR INSERT WITH CHECK (iam.is_internal_service() OR EXISTS (SELECT 1 FROM reliability.calculation_run r WHERE r.id = calculation_run_id AND iam.can_access_mtbf_plan(r.plan_id, true)));

CREATE POLICY capability_select ON reliability.plan_capability_assignment
    FOR SELECT USING (iam.is_system_admin() OR user_id = iam.current_user_id());
CREATE POLICY capability_insert ON reliability.plan_capability_assignment
    FOR INSERT WITH CHECK (iam.is_system_admin());

-- Existing calculation policies must allow the internal deterministic worker.
DROP POLICY IF EXISTS calculation_select ON reliability.calculation_run;
CREATE POLICY calculation_select ON reliability.calculation_run
    FOR SELECT USING (iam.is_internal_service() OR iam.can_access_calculation(id, false) OR (plan_id IS NOT NULL AND iam.can_access_mtbf_plan(plan_id, false)));
DROP POLICY IF EXISTS calculation_admin_write ON reliability.calculation_run;
CREATE POLICY calculation_write ON reliability.calculation_run
    FOR ALL USING (iam.is_internal_service() OR iam.is_system_admin() OR (plan_id IS NOT NULL AND iam.can_access_mtbf_plan(plan_id, true)))
    WITH CHECK (iam.is_internal_service() OR iam.is_system_admin() OR (plan_id IS NOT NULL AND iam.can_access_mtbf_plan(plan_id, true)));

DROP POLICY IF EXISTS recompute_admin ON reliability.recompute_job;
CREATE POLICY recompute_worker ON reliability.recompute_job
    FOR ALL USING (iam.is_internal_service() OR iam.is_system_admin())
    WITH CHECK (iam.is_internal_service() OR iam.is_system_admin());

-- Audit all new mutable facts and revision inserts.
DO $$
DECLARE
    v_table text;
BEGIN
    FOREACH v_table IN ARRAY ARRAY[
        'reliability.mtbf_test_plan', 'reliability.mtbf_plan_evaluation',
        'reliability.mtbf_plan_campaign', 'reliability.member_selection_revision',
        'reliability.mtbf_plan_member', 'reliability.plan_control_revision',
        'reliability.failure_case', 'reliability.failure_case_event',
        'reliability.failure_case_relation', 'reliability.failure_adjudication_revision',
        'reliability.exposure_slice', 'reliability.exposure_disposition_revision',
        'reliability.calculation_member_result', 'reliability.calculation_input_ref',
        'reliability.plan_gate_evaluation_revision', 'reliability.formal_conclusion_revision',
        'reliability.plan_state_transition', 'reliability.plan_hold_revision',
        'reliability.plan_capability_assignment'
    ] LOOP
        EXECUTE format(
            'CREATE TRIGGER trg_audit_row_change AFTER INSERT OR UPDATE OR DELETE ON %s '
            'FOR EACH ROW EXECUTE FUNCTION audit.capture_row_change()',
            v_table
        );
    END LOOP;
END;
$$;

-- Publish the V1 calculation method and production scope without rewriting drafts.
INSERT INTO reliability.statistics_method (
    method_code, version, name, asset_kind, parameter_schema,
    default_parameters, implementation_key, status
) VALUES (
    'POISSON_FIXED_TIME_CENSORED',
    '1.0',
    '定时截尾泊松过程 MTBF',
    'WHOLE_MACHINE',
    '{"type":"object","additionalProperties":false,"properties":{"confidence_levels":{"type":"array","items":{"type":"number","exclusiveMinimum":0,"exclusiveMaximum":1}},"acceptance_failure_count":{"type":"integer","minimum":0}},"required":["confidence_levels","acceptance_failure_count"]}'::jsonb,
    '{"confidence_levels":[0.7,0.9],"acceptance_failure_count":2}'::jsonb,
    'mtbf.poisson.fixed_time_censored.v1',
    'PUBLISHED'
) ON CONFLICT (method_code, version) DO NOTHING;

INSERT INTO reliability.mtbf_scope (
    scope_code, name, asset_kind, version, description, status, published_at
) VALUES (
    'OVERALL', '整机整体 MTBF', 'WHOLE_MACHINE', '1.0',
    'RP1正式整机G2总体评价范围', 'PUBLISHED', now()
) ON CONFLICT (scope_code, asset_kind, version) DO NOTHING;

-- Local Docker identities. They have no credentials; the development API
-- header mode is the only caller allowed to use the local human identity.
INSERT INTO iam.app_user (
    public_id, username, display_name, role, principal_kind, enabled, must_change_password
)
SELECT '00000000-0000-7000-8000-000000000001'::uuid,
       'rp1-recompute-worker', 'MTBF重算服务', 'TEST_EXECUTOR', 'SERVICE', true, false
WHERE NOT EXISTS (SELECT 1 FROM iam.app_user WHERE username = 'rp1-recompute-worker');

INSERT INTO iam.app_user (
    public_id, username, display_name, role, principal_kind, enabled, must_change_password
)
SELECT '00000000-0000-7000-8000-000000000002'::uuid,
       'local-admin', '本地开发管理员', 'SYSTEM_ADMIN', 'HUMAN', true, false
WHERE NOT EXISTS (SELECT 1 FROM iam.app_user WHERE username = 'local-admin');

GRANT USAGE ON SCHEMA reliability TO __RP1_APP_USER__, __RP1_READONLY_USER__;
GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA reliability TO __RP1_APP_USER__;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA reliability TO __RP1_APP_USER__;
GRANT SELECT ON ALL TABLES IN SCHEMA reliability TO __RP1_READONLY_USER__;
GRANT EXECUTE ON FUNCTION iam.current_principal_kind() TO __RP1_APP_USER__, __RP1_READONLY_USER__;
GRANT EXECUTE ON FUNCTION iam.is_internal_service() TO __RP1_APP_USER__, __RP1_READONLY_USER__;
GRANT EXECUTE ON FUNCTION iam.has_plan_capability(bigint, text) TO __RP1_APP_USER__, __RP1_READONLY_USER__;
GRANT EXECUTE ON FUNCTION iam.can_access_mtbf_plan(bigint, boolean) TO __RP1_APP_USER__, __RP1_READONLY_USER__;
GRANT EXECUTE ON FUNCTION reliability.claim_recompute_job(text) TO __RP1_APP_USER__;
GRANT EXECUTE ON FUNCTION reliability.enqueue_evaluation_recompute(bigint, bigint, text) TO __RP1_APP_USER__;

REVOKE UPDATE, DELETE ON
    reliability.member_selection_revision,
    reliability.plan_control_revision,
    reliability.failure_adjudication_revision,
    reliability.exposure_disposition_revision,
    reliability.calculation_member_result,
    reliability.calculation_input_ref,
    reliability.plan_gate_evaluation_revision,
    reliability.formal_conclusion_revision,
    reliability.plan_state_transition,
    reliability.plan_hold_revision
FROM __RP1_APP_USER__, __RP1_READONLY_USER__;
