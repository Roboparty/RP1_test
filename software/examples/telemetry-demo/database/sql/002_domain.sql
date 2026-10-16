CREATE SCHEMA iam;
CREATE SCHEMA catalog;
CREATE SCHEMA test;
CREATE SCHEMA reliability;
CREATE SCHEMA health;
CREATE SCHEMA integration;
CREATE SCHEMA audit;

REVOKE CREATE ON SCHEMA public FROM PUBLIC;
REVOKE ALL ON SCHEMA iam, catalog, test, reliability, health, integration, audit FROM PUBLIC;

CREATE OR REPLACE FUNCTION public.set_updated_at()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    NEW.updated_at := clock_timestamp();
    RETURN NEW;
END;
$$;

-- ---------------------------------------------------------------------------
-- IAM
-- ---------------------------------------------------------------------------

CREATE TABLE iam.app_user (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    public_id uuid NOT NULL DEFAULT public.uuid_v7() UNIQUE,
    username text NOT NULL,
    display_name text NOT NULL,
    role text NOT NULL CHECK (role IN ('VIEWER', 'TEST_EXECUTOR', 'SYSTEM_ADMIN')),
    enabled boolean NOT NULL DEFAULT true,
    must_change_password boolean NOT NULL DEFAULT true,
    external_identity text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    disabled_at timestamptz
);

CREATE UNIQUE INDEX uq_app_user_username_lower ON iam.app_user (lower(username));

CREATE TABLE iam.user_credential (
    user_id bigint PRIMARY KEY REFERENCES iam.app_user(id) ON DELETE CASCADE,
    password_hash text NOT NULL,
    hash_algorithm text NOT NULL DEFAULT 'argon2id',
    password_changed_at timestamptz NOT NULL DEFAULT now(),
    failed_attempts integer NOT NULL DEFAULT 0 CHECK (failed_attempts >= 0),
    locked_until timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE iam.auth_session (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    public_id uuid NOT NULL DEFAULT public.uuid_v7() UNIQUE,
    user_id bigint NOT NULL REFERENCES iam.app_user(id),
    token_hash text NOT NULL UNIQUE,
    csrf_token_hash text NOT NULL DEFAULT '',
    session_type text NOT NULL CHECK (session_type IN ('BROWSER', 'STATION', 'SERVICE')),
    expires_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    last_seen_at timestamptz NOT NULL DEFAULT now(),
    revoked_at timestamptz,
    CHECK (expires_at > created_at)
);

-- ---------------------------------------------------------------------------
-- Integration sources required by test facts
-- ---------------------------------------------------------------------------

CREATE TABLE integration.ingestion_source (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    public_id uuid NOT NULL DEFAULT public.uuid_v7() UNIQUE,
    source_code text NOT NULL UNIQUE,
    source_type text NOT NULL CHECK (
        source_type IN ('API_COLLECTOR', 'MANUAL', 'LEGACY_IMPORT', 'INFLUX_REFERENCE', 'SYSTEM')
    ),
    direction text NOT NULL DEFAULT 'INBOUND' CHECK (direction IN ('INBOUND', 'INTERNAL')),
    status text NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE', 'DEGRADED', 'DISABLED')),
    config_reference text NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- Physical organization and assets
-- ---------------------------------------------------------------------------

CREATE TABLE test.site (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    public_id uuid NOT NULL DEFAULT public.uuid_v7() UNIQUE,
    site_code text NOT NULL UNIQUE,
    name text NOT NULL,
    location text NOT NULL DEFAULT '',
    enabled boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE test.lab (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    public_id uuid NOT NULL DEFAULT public.uuid_v7() UNIQUE,
    site_id bigint NOT NULL REFERENCES test.site(id),
    lab_code text NOT NULL,
    name text NOT NULL,
    enabled boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (site_id, lab_code)
);

CREATE TABLE test.station (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    public_id uuid NOT NULL DEFAULT public.uuid_v7() UNIQUE,
    lab_id bigint NOT NULL REFERENCES test.lab(id),
    station_code text NOT NULL UNIQUE,
    name text NOT NULL,
    station_type text NOT NULL CHECK (station_type IN ('WHOLE_MACHINE', 'MODULE', 'SHARED')),
    enabled boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE test.manufacturing_batch (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    public_id uuid NOT NULL DEFAULT public.uuid_v7() UNIQUE,
    batch_code text NOT NULL UNIQUE,
    asset_kind text NOT NULL CHECK (asset_kind IN ('WHOLE_MACHINE', 'MODULE')),
    product_family text NOT NULL,
    manufactured_at date,
    notes text NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE test.asset (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    public_id uuid NOT NULL DEFAULT public.uuid_v7() UNIQUE,
    asset_code text NOT NULL UNIQUE,
    asset_kind text NOT NULL CHECK (asset_kind IN ('WHOLE_MACHINE', 'MODULE')),
    product_family text NOT NULL,
    batch_id bigint REFERENCES test.manufacturing_batch(id),
    serial_number text NOT NULL,
    lifecycle_status text NOT NULL DEFAULT 'CANDIDATE' CHECK (
        lifecycle_status IN ('CANDIDATE', 'ACTIVE', 'PAUSED', 'MAINTENANCE', 'EXITED', 'RETIRED', 'VOIDED')
    ),
    display_name text NOT NULL DEFAULT '',
    notes text NOT NULL DEFAULT '',
    voided boolean NOT NULL DEFAULT false,
    void_reason text NOT NULL DEFAULT '',
    voided_by bigint REFERENCES iam.app_user(id),
    voided_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (asset_kind, product_family, serial_number),
    CHECK (NOT voided OR (voided_at IS NOT NULL AND btrim(void_reason) <> ''))
);

CREATE TABLE test.whole_machine_profile (
    asset_id bigint PRIMARY KEY REFERENCES test.asset(id),
    model text NOT NULL,
    platform_generation text NOT NULL DEFAULT '',
    nominal_payload_kg numeric(12,3),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE test.module_profile (
    asset_id bigint PRIMARY KEY REFERENCES test.asset(id),
    module_type text NOT NULL,
    part_number text NOT NULL DEFAULT '',
    rated_specification jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE test.asset_relationship (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    public_id uuid NOT NULL DEFAULT public.uuid_v7() UNIQUE,
    parent_asset_id bigint NOT NULL REFERENCES test.asset(id),
    child_asset_id bigint NOT NULL REFERENCES test.asset(id),
    relationship_type text NOT NULL CHECK (
        relationship_type IN ('INSTALLED_IN', 'REMOVED_FROM', 'SOURCED_FROM')
    ),
    valid_from timestamptz NOT NULL,
    valid_to timestamptz,
    source text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    CHECK (parent_asset_id <> child_asset_id),
    CHECK (valid_to IS NULL OR valid_to >= valid_from)
);

CREATE TABLE test.configuration_snapshot (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    public_id uuid NOT NULL DEFAULT public.uuid_v7() UNIQUE,
    asset_id bigint NOT NULL REFERENCES test.asset(id),
    fingerprint text NOT NULL,
    hardware_manifest jsonb NOT NULL DEFAULT '{}'::jsonb,
    software_manifest jsonb NOT NULL DEFAULT '{}'::jsonb,
    parameter_manifest jsonb NOT NULL DEFAULT '{}'::jsonb,
    reliability_impact text NOT NULL DEFAULT 'EQUIVALENT' CHECK (
        reliability_impact IN ('EQUIVALENT', 'POTENTIALLY_SIGNIFICANT', 'UNKNOWN')
    ),
    captured_from text NOT NULL,
    captured_by bigint REFERENCES iam.app_user(id),
    effective_from timestamptz NOT NULL,
    supersedes_snapshot_id bigint REFERENCES test.configuration_snapshot(id),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (asset_id, fingerprint)
);

-- ---------------------------------------------------------------------------
-- Programs, campaigns and authorization scope
-- ---------------------------------------------------------------------------

CREATE TABLE test.test_program (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    public_id uuid NOT NULL DEFAULT public.uuid_v7() UNIQUE,
    program_code text NOT NULL UNIQUE,
    name text NOT NULL,
    objective text NOT NULL DEFAULT '',
    status text NOT NULL CHECK (status IN ('DRAFT', 'ACTIVE', 'CLOSED', 'CANCELLED')),
    owner_id bigint REFERENCES iam.app_user(id),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE test.test_campaign (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    public_id uuid NOT NULL DEFAULT public.uuid_v7() UNIQUE,
    program_id bigint NOT NULL REFERENCES test.test_program(id),
    site_id bigint REFERENCES test.site(id),
    campaign_code text NOT NULL,
    name text NOT NULL,
    asset_kind text NOT NULL CHECK (asset_kind IN ('WHOLE_MACHINE', 'MODULE')),
    status text NOT NULL CHECK (status IN ('PLANNED', 'ACTIVE', 'PAUSED', 'CLOSED', 'CANCELLED')),
    planned_start timestamptz,
    planned_end timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (program_id, campaign_code),
    CHECK (planned_end IS NULL OR planned_start IS NULL OR planned_end >= planned_start)
);

CREATE TABLE test.campaign_asset (
    campaign_id bigint NOT NULL REFERENCES test.test_campaign(id),
    asset_id bigint NOT NULL REFERENCES test.asset(id),
    participation_role text NOT NULL DEFAULT 'PRIMARY' CHECK (
        participation_role IN ('PRIMARY', 'REFERENCE', 'OTHER')
    ),
    joined_at timestamptz NOT NULL DEFAULT now(),
    left_at timestamptz,
    PRIMARY KEY (campaign_id, asset_id),
    CHECK (left_at IS NULL OR left_at >= joined_at)
);

CREATE TABLE iam.user_campaign_access (
    user_id bigint NOT NULL REFERENCES iam.app_user(id) ON DELETE CASCADE,
    campaign_id bigint NOT NULL REFERENCES test.test_campaign(id) ON DELETE CASCADE,
    access_level text NOT NULL CHECK (access_level IN ('VIEW', 'EDIT')),
    granted_by bigint NOT NULL REFERENCES iam.app_user(id),
    granted_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, campaign_id)
);

-- ---------------------------------------------------------------------------
-- Catalogs and test methods
-- ---------------------------------------------------------------------------

CREATE TABLE catalog.mission_tag (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    public_id uuid NOT NULL DEFAULT public.uuid_v7() UNIQUE,
    tag_code text NOT NULL UNIQUE,
    name text NOT NULL,
    description text NOT NULL DEFAULT '',
    enabled boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE catalog.test_case (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    public_id uuid NOT NULL DEFAULT public.uuid_v7() UNIQUE,
    case_code text NOT NULL UNIQUE,
    name text NOT NULL,
    asset_kind text NOT NULL CHECK (asset_kind IN ('WHOLE_MACHINE', 'MODULE')),
    domain text NOT NULL,
    evidence_type text NOT NULL,
    enabled boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE catalog.test_case_version (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    public_id uuid NOT NULL DEFAULT public.uuid_v7() UNIQUE,
    test_case_id bigint NOT NULL REFERENCES catalog.test_case(id),
    version text NOT NULL,
    procedure_spec jsonb NOT NULL,
    stage_definitions jsonb NOT NULL,
    equipment_requirements jsonb NOT NULL DEFAULT '{}'::jsonb,
    metric_requirements jsonb NOT NULL DEFAULT '[]'::jsonb,
    sampling_requirements jsonb NOT NULL DEFAULT '{}'::jsonb,
    fault_rules jsonb NOT NULL DEFAULT '{}'::jsonb,
    termination_rules jsonb NOT NULL DEFAULT '{}'::jsonb,
    decision_rules jsonb NOT NULL DEFAULT '{}'::jsonb,
    source_references jsonb NOT NULL DEFAULT '[]'::jsonb,
    deviation_notes text NOT NULL DEFAULT '',
    status text NOT NULL CHECK (status IN ('DRAFT', 'PUBLISHED', 'RETIRED')),
    published_by bigint REFERENCES iam.app_user(id),
    published_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (test_case_id, version)
);

CREATE TABLE catalog.configuration_equivalence_rule (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    public_id uuid NOT NULL DEFAULT public.uuid_v7() UNIQUE,
    rule_code text NOT NULL,
    version text NOT NULL,
    asset_kind text NOT NULL CHECK (asset_kind IN ('WHOLE_MACHINE', 'MODULE')),
    match_spec jsonb NOT NULL,
    outcome text NOT NULL CHECK (outcome IN ('INHERIT_POPULATION', 'REQUIRE_REVIEW')),
    status text NOT NULL CHECK (status IN ('DRAFT', 'PUBLISHED', 'RETIRED')),
    published_by bigint REFERENCES iam.app_user(id),
    published_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (rule_code, version)
);

CREATE TABLE test.configuration_change (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    public_id uuid NOT NULL DEFAULT public.uuid_v7() UNIQUE,
    asset_id bigint NOT NULL REFERENCES test.asset(id),
    from_snapshot_id bigint REFERENCES test.configuration_snapshot(id),
    to_snapshot_id bigint NOT NULL REFERENCES test.configuration_snapshot(id),
    change_type text NOT NULL CHECK (
        change_type IN ('SOFTWARE', 'HARDWARE', 'PARAMETER', 'MIXED', 'DATA_CORRECTION')
    ),
    assessment text NOT NULL CHECK (
        assessment IN ('MINOR_EQUIVALENT', 'SIGNIFICANT', 'UNKNOWN', 'CORRECTION')
    ),
    matched_rule_id bigint REFERENCES catalog.configuration_equivalence_rule(id),
    changed_at timestamptz NOT NULL,
    description text NOT NULL DEFAULT '',
    recorded_by bigint REFERENCES iam.app_user(id),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK (from_snapshot_id IS NULL OR from_snapshot_id <> to_snapshot_id)
);

CREATE TABLE test.campaign_coverage_item (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    public_id uuid NOT NULL DEFAULT public.uuid_v7() UNIQUE,
    campaign_id bigint NOT NULL REFERENCES test.test_campaign(id),
    test_case_version_id bigint NOT NULL REFERENCES catalog.test_case_version(id),
    target_asset_id bigint REFERENCES test.asset(id),
    target_batch_id bigint REFERENCES test.manufacturing_batch(id),
    target_configuration_selector jsonb NOT NULL DEFAULT '{}'::jsonb,
    planned_runs integer NOT NULL DEFAULT 1 CHECK (planned_runs > 0),
    status text NOT NULL DEFAULT 'NOT_STARTED' CHECK (
        status IN ('NOT_STARTED', 'IN_PROGRESS', 'BLOCKED', 'COMPLETE', 'CANCELLED')
    ),
    cancellation_reason text NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- Long-running cycles, executions, stages and time ledger
-- ---------------------------------------------------------------------------

CREATE TABLE test.test_cycle (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    public_id uuid NOT NULL DEFAULT public.uuid_v7() UNIQUE,
    cycle_code text NOT NULL UNIQUE,
    campaign_id bigint NOT NULL REFERENCES test.test_campaign(id),
    asset_id bigint NOT NULL REFERENCES test.asset(id),
    status text NOT NULL CHECK (status IN ('PLANNED', 'ACTIVE', 'PAUSED', 'CLOSED', 'EXITED', 'VOIDED')),
    baseline_confirmed_at timestamptz,
    started_at timestamptz,
    ended_at timestamptz,
    close_reason text NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK (ended_at IS NULL OR started_at IS NULL OR ended_at >= started_at)
);

CREATE TABLE test.analysis_segment (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    public_id uuid NOT NULL DEFAULT public.uuid_v7() UNIQUE,
    campaign_id bigint NOT NULL REFERENCES test.test_campaign(id),
    cycle_id bigint NOT NULL REFERENCES test.test_cycle(id),
    configuration_snapshot_id bigint NOT NULL REFERENCES test.configuration_snapshot(id),
    segment_no integer NOT NULL CHECK (segment_no > 0),
    reason text NOT NULL CHECK (
        reason IN ('ORIGINAL', 'REPAIR', 'COMPONENT_CHANGE', 'CONFIG_CHANGE', 'MANUAL')
    ),
    change_event_id bigint,
    started_at timestamptz NOT NULL,
    ended_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (cycle_id, segment_no),
    CHECK (ended_at IS NULL OR ended_at >= started_at)
);

CREATE TABLE test.test_execution (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    public_id uuid NOT NULL DEFAULT public.uuid_v7() UNIQUE,
    execution_code text NOT NULL UNIQUE,
    campaign_id bigint NOT NULL REFERENCES test.test_campaign(id),
    cycle_id bigint NOT NULL REFERENCES test.test_cycle(id),
    asset_id bigint NOT NULL REFERENCES test.asset(id),
    configuration_snapshot_id bigint NOT NULL REFERENCES test.configuration_snapshot(id),
    test_case_version_id bigint NOT NULL REFERENCES catalog.test_case_version(id),
    station_id bigint REFERENCES test.station(id),
    operator_id bigint REFERENCES iam.app_user(id),
    source_id bigint REFERENCES integration.ingestion_source(id),
    source_execution_key text,
    status text NOT NULL CHECK (
        status IN ('SCHEDULED', 'RUNNING', 'PAUSED', 'COMPLETED', 'FAILED', 'BLOCKED', 'CANCELLED', 'VOIDED')
    ),
    source_time timestamptz,
    received_at timestamptz,
    normalized_started_at timestamptz,
    normalized_ended_at timestamptz,
    clock_quality text NOT NULL DEFAULT 'VALID' CHECK (clock_quality IN ('VALID', 'PARTIAL', 'INVALID')),
    data_quality text NOT NULL DEFAULT 'VALID' CHECK (data_quality IN ('VALID', 'PARTIAL', 'INVALID', 'MISSING')),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK (
        normalized_ended_at IS NULL OR normalized_started_at IS NULL OR
        normalized_ended_at >= normalized_started_at
    ),
    CHECK ((source_id IS NULL) = (source_execution_key IS NULL))
);

CREATE TABLE test.execution_stage (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    public_id uuid NOT NULL DEFAULT public.uuid_v7() UNIQUE,
    campaign_id bigint NOT NULL REFERENCES test.test_campaign(id),
    execution_id bigint NOT NULL REFERENCES test.test_execution(id),
    stage_code text NOT NULL,
    stage_name text NOT NULL,
    sequence_no integer NOT NULL CHECK (sequence_no > 0),
    status text NOT NULL CHECK (
        status IN ('SCHEDULED', 'RUNNING', 'PAUSED', 'COMPLETED', 'FAILED', 'BLOCKED', 'CANCELLED')
    ),
    source_time timestamptz,
    received_at timestamptz,
    normalized_started_at timestamptz,
    normalized_ended_at timestamptz,
    clock_quality text NOT NULL DEFAULT 'VALID' CHECK (clock_quality IN ('VALID', 'PARTIAL', 'INVALID')),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (execution_id, sequence_no),
    CHECK (
        normalized_ended_at IS NULL OR normalized_started_at IS NULL OR
        normalized_ended_at >= normalized_started_at
    )
);

CREATE TABLE test.stage_mission_tag (
    stage_id bigint NOT NULL REFERENCES test.execution_stage(id) ON DELETE CASCADE,
    mission_tag_id bigint NOT NULL REFERENCES catalog.mission_tag(id),
    assigned_by bigint REFERENCES iam.app_user(id),
    assigned_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (stage_id, mission_tag_id)
);

CREATE TABLE test.runtime_interval (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    public_id uuid NOT NULL DEFAULT public.uuid_v7() UNIQUE,
    campaign_id bigint NOT NULL REFERENCES test.test_campaign(id),
    asset_id bigint NOT NULL REFERENCES test.asset(id),
    cycle_id bigint REFERENCES test.test_cycle(id),
    execution_id bigint REFERENCES test.test_execution(id),
    stage_id bigint REFERENCES test.execution_stage(id),
    source_id bigint REFERENCES integration.ingestion_source(id),
    source_kind text NOT NULL CHECK (source_kind IN ('LEGACY_REPORTED', 'NATIVE', 'MANUAL')),
    source_record_key text,
    source_time timestamptz,
    received_at timestamptz,
    started_at timestamptz,
    ended_at timestamptz,
    active_seconds numeric(20,3) NOT NULL CHECK (active_seconds >= 0),
    clock_quality text NOT NULL CHECK (clock_quality IN ('VALID', 'PARTIAL', 'INVALID')),
    data_quality text NOT NULL CHECK (data_quality IN ('VALID', 'PARTIAL', 'INVALID', 'MISSING')),
    source_method text NOT NULL,
    voided boolean NOT NULL DEFAULT false,
    void_reason text NOT NULL DEFAULT '',
    voided_by bigint REFERENCES iam.app_user(id),
    voided_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK ((source_id IS NULL) = (source_record_key IS NULL)),
    CHECK (ended_at IS NULL OR started_at IS NULL OR ended_at >= started_at),
    CHECK (
        source_kind = 'LEGACY_REPORTED' OR
        (started_at IS NOT NULL AND ended_at IS NOT NULL)
    ),
    CHECK (
        started_at IS NULL OR ended_at IS NULL OR
        active_seconds <= extract(epoch FROM (ended_at - started_at)) + 0.001
    ),
    CHECK (NOT voided OR (voided_at IS NOT NULL AND btrim(void_reason) <> '')),
    EXCLUDE USING gist (
        asset_id WITH =,
        tstzrange(started_at, ended_at, '[)') WITH &&
    ) WHERE (
        NOT voided AND
        source_kind IN ('NATIVE', 'MANUAL') AND
        started_at IS NOT NULL AND
        ended_at IS NOT NULL
    )
);

CREATE TABLE test.test_event (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    public_id uuid NOT NULL DEFAULT public.uuid_v7() UNIQUE,
    campaign_id bigint NOT NULL REFERENCES test.test_campaign(id),
    asset_id bigint NOT NULL REFERENCES test.asset(id),
    cycle_id bigint REFERENCES test.test_cycle(id),
    execution_id bigint REFERENCES test.test_execution(id),
    stage_id bigint REFERENCES test.execution_stage(id),
    event_type text NOT NULL,
    source_time timestamptz,
    received_at timestamptz NOT NULL DEFAULT now(),
    normalized_time timestamptz NOT NULL,
    clock_quality text NOT NULL CHECK (clock_quality IN ('VALID', 'PARTIAL', 'INVALID')),
    data_quality text NOT NULL CHECK (data_quality IN ('VALID', 'PARTIAL', 'INVALID', 'MISSING')),
    source_id bigint REFERENCES integration.ingestion_source(id),
    source_event_key text,
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    voided boolean NOT NULL DEFAULT false,
    void_reason text NOT NULL DEFAULT '',
    voided_by bigint REFERENCES iam.app_user(id),
    voided_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK ((source_id IS NULL) = (source_event_key IS NULL)),
    CHECK (NOT voided OR (voided_at IS NOT NULL AND btrim(void_reason) <> ''))
);

ALTER TABLE test.analysis_segment
    ADD CONSTRAINT fk_analysis_segment_change_event
    FOREIGN KEY (change_event_id) REFERENCES test.test_event(id);

-- ---------------------------------------------------------------------------
-- Error, metric and health policy catalogs
-- ---------------------------------------------------------------------------

CREATE TABLE catalog.error_catalog_version (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    public_id uuid NOT NULL DEFAULT public.uuid_v7() UNIQUE,
    version text NOT NULL UNIQUE,
    status text NOT NULL CHECK (status IN ('DRAFT', 'PUBLISHED', 'RETIRED')),
    published_by bigint REFERENCES iam.app_user(id),
    published_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE catalog.error_code (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    public_id uuid NOT NULL DEFAULT public.uuid_v7() UNIQUE,
    catalog_version_id bigint NOT NULL REFERENCES catalog.error_catalog_version(id),
    unified_code text NOT NULL,
    domain text NOT NULL CHECK (domain IN ('SOFTWARE', 'HARDWARE')),
    subsystem text NOT NULL,
    category text NOT NULL,
    title text NOT NULL,
    description text NOT NULL DEFAULT '',
    default_classification text CHECK (
        default_classification IS NULL OR
        default_classification IN ('FAILED', 'BLOCKED', 'NON_RELEVANT')
    ),
    enabled boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (catalog_version_id, unified_code)
);

CREATE TABLE catalog.source_error_mapping (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_id bigint NOT NULL REFERENCES integration.ingestion_source(id),
    source_raw_code text NOT NULL,
    error_code_id bigint NOT NULL REFERENCES catalog.error_code(id),
    valid_from timestamptz NOT NULL DEFAULT now(),
    valid_to timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (source_id, source_raw_code, valid_from),
    CHECK (valid_to IS NULL OR valid_to >= valid_from)
);

CREATE TABLE catalog.metric_definition (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    public_id uuid NOT NULL DEFAULT public.uuid_v7() UNIQUE,
    metric_code text NOT NULL UNIQUE,
    canonical_name text NOT NULL,
    aliases text[] NOT NULL DEFAULT '{}',
    domain text NOT NULL,
    enabled boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE catalog.metric_version (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    public_id uuid NOT NULL DEFAULT public.uuid_v7() UNIQUE,
    metric_definition_id bigint NOT NULL REFERENCES catalog.metric_definition(id),
    version text NOT NULL,
    canonical_unit text NOT NULL,
    value_type text NOT NULL CHECK (value_type IN ('NUMBER', 'BOOLEAN', 'CATEGORY', 'VECTOR')),
    calculation_key text NOT NULL,
    calculation_spec jsonb NOT NULL DEFAULT '{}'::jsonb,
    aggregation_spec jsonb NOT NULL DEFAULT '{}'::jsonb,
    conversion_spec jsonb NOT NULL DEFAULT '{}'::jsonb,
    direction text NOT NULL CHECK (
        direction IN ('HIGHER_BETTER', 'LOWER_BETTER', 'TARGET_RANGE', 'DESCRIPTIVE')
    ),
    comparability_spec jsonb NOT NULL DEFAULT '{}'::jsonb,
    formal_eligible boolean NOT NULL DEFAULT false,
    status text NOT NULL CHECK (status IN ('DRAFT', 'PUBLISHED', 'RETIRED')),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (metric_definition_id, version)
);

CREATE TABLE catalog.health_check_policy (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    public_id uuid NOT NULL DEFAULT public.uuid_v7() UNIQUE,
    policy_code text NOT NULL,
    version text NOT NULL,
    asset_kind text NOT NULL CHECK (asset_kind IN ('WHOLE_MACHINE', 'MODULE')),
    check_level text NOT NULL CHECK (check_level IN ('F0', 'F1', 'F2')),
    trigger_mode text NOT NULL DEFAULT 'ANY' CHECK (trigger_mode IN ('ANY', 'ALL')),
    method_spec jsonb NOT NULL DEFAULT '{}'::jsonb,
    status text NOT NULL CHECK (status IN ('DRAFT', 'PUBLISHED', 'RETIRED')),
    published_by bigint REFERENCES iam.app_user(id),
    published_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (policy_code, version)
);

CREATE TABLE catalog.health_check_trigger (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    policy_id bigint NOT NULL REFERENCES catalog.health_check_policy(id) ON DELETE CASCADE,
    trigger_type text NOT NULL CHECK (
        trigger_type IN ('TIME', 'DISTANCE', 'STEPS', 'GRAB_COUNT', 'TASK_CYCLES', 'BATTERY_CYCLES', 'EVENT')
    ),
    threshold_value numeric(20,6),
    threshold_unit text,
    event_code text,
    trigger_group integer NOT NULL DEFAULT 1 CHECK (trigger_group > 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    CHECK (
        (trigger_type = 'EVENT' AND event_code IS NOT NULL) OR
        (trigger_type <> 'EVENT' AND threshold_value IS NOT NULL AND threshold_value > 0)
    )
);

CREATE TABLE catalog.health_check_requirement (
    policy_id bigint NOT NULL REFERENCES catalog.health_check_policy(id) ON DELETE CASCADE,
    metric_version_id bigint NOT NULL REFERENCES catalog.metric_version(id),
    required boolean NOT NULL DEFAULT true,
    method_override jsonb NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (policy_id, metric_version_id)
);

-- ---------------------------------------------------------------------------
-- Reliability populations, scopes, exposure and interruption facts
-- ---------------------------------------------------------------------------

CREATE TABLE reliability.mtbf_population (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    public_id uuid NOT NULL DEFAULT public.uuid_v7() UNIQUE,
    population_code text NOT NULL UNIQUE,
    name text NOT NULL,
    asset_kind text NOT NULL CHECK (asset_kind IN ('WHOLE_MACHINE', 'MODULE')),
    status text NOT NULL CHECK (status IN ('DRAFT', 'ACTIVE', 'CLOSED')),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE reliability.population_campaign (
    population_id bigint NOT NULL REFERENCES reliability.mtbf_population(id) ON DELETE CASCADE,
    campaign_id bigint NOT NULL REFERENCES test.test_campaign(id) ON DELETE CASCADE,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (population_id, campaign_id)
);

CREATE TABLE reliability.population_membership (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    public_id uuid NOT NULL DEFAULT public.uuid_v7() UNIQUE,
    population_id bigint NOT NULL REFERENCES reliability.mtbf_population(id),
    configuration_snapshot_id bigint NOT NULL REFERENCES test.configuration_snapshot(id),
    status text NOT NULL CHECK (status IN ('PENDING', 'INCLUDED', 'EXCLUDED', 'CLOSED')),
    decision_source text NOT NULL CHECK (decision_source IN ('INHERITED_RULE', 'MANUAL_REVIEW')),
    matched_rule_id bigint REFERENCES catalog.configuration_equivalence_rule(id),
    rationale text NOT NULL DEFAULT '',
    reviewed_by bigint REFERENCES iam.app_user(id),
    reviewed_at timestamptz,
    valid_from timestamptz NOT NULL,
    valid_to timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK (valid_to IS NULL OR valid_to >= valid_from)
);

CREATE UNIQUE INDEX uq_population_active_membership
    ON reliability.population_membership (population_id, configuration_snapshot_id)
    WHERE status IN ('PENDING', 'INCLUDED');

CREATE TABLE reliability.configuration_equivalence_review (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    public_id uuid NOT NULL DEFAULT public.uuid_v7() UNIQUE,
    campaign_id bigint NOT NULL REFERENCES test.test_campaign(id),
    configuration_snapshot_id bigint NOT NULL REFERENCES test.configuration_snapshot(id),
    target_population_id bigint NOT NULL REFERENCES reliability.mtbf_population(id),
    status text NOT NULL CHECK (status IN ('PENDING', 'APPROVED', 'REJECTED')),
    difference_summary jsonb NOT NULL DEFAULT '{}'::jsonb,
    reliability_assessment text NOT NULL DEFAULT '',
    include_pending_exposure boolean NOT NULL DEFAULT true,
    submitted_by bigint REFERENCES iam.app_user(id),
    submitted_at timestamptz NOT NULL DEFAULT now(),
    decided_by bigint REFERENCES iam.app_user(id),
    decided_at timestamptz,
    decision_reason text NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE reliability.mtbf_scope (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    public_id uuid NOT NULL DEFAULT public.uuid_v7() UNIQUE,
    scope_code text NOT NULL,
    name text NOT NULL,
    asset_kind text NOT NULL CHECK (asset_kind IN ('WHOLE_MACHINE', 'MODULE')),
    version text NOT NULL,
    description text NOT NULL DEFAULT '',
    status text NOT NULL CHECK (status IN ('DRAFT', 'PUBLISHED', 'RETIRED')),
    published_by bigint REFERENCES iam.app_user(id),
    published_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (scope_code, asset_kind, version)
);

CREATE TABLE reliability.mtbf_scope_rule (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    scope_id bigint NOT NULL REFERENCES reliability.mtbf_scope(id) ON DELETE CASCADE,
    rule_order integer NOT NULL CHECK (rule_order > 0),
    include boolean NOT NULL,
    mission_tag_id bigint REFERENCES catalog.mission_tag(id),
    evidence_type_pattern text NOT NULL DEFAULT '*',
    condition_spec jsonb NOT NULL DEFAULT '{}'::jsonb,
    rationale text NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (scope_id, rule_order)
);

CREATE TABLE reliability.exposure_assessment (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    public_id uuid NOT NULL DEFAULT public.uuid_v7() UNIQUE,
    campaign_id bigint NOT NULL REFERENCES test.test_campaign(id),
    asset_id bigint NOT NULL REFERENCES test.asset(id),
    runtime_interval_id bigint NOT NULL UNIQUE REFERENCES test.runtime_interval(id),
    configuration_snapshot_id bigint REFERENCES test.configuration_snapshot(id),
    eligible boolean NOT NULL DEFAULT false,
    assignment_status text NOT NULL CHECK (
        assignment_status IN ('CONFIRMED', 'PENDING_CONFIG_REVIEW', 'INVALID')
    ),
    quality_status text NOT NULL CHECK (quality_status IN ('VALID', 'PARTIAL', 'INVALID', 'MISSING')),
    assessment_method text NOT NULL,
    assessment_rule_version text NOT NULL DEFAULT '',
    exclusion_reason text NOT NULL DEFAULT '',
    assessed_by bigint REFERENCES iam.app_user(id),
    assessed_at timestamptz NOT NULL DEFAULT now(),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK (
        NOT eligible OR
        (assignment_status = 'CONFIRMED' AND quality_status IN ('VALID', 'PARTIAL'))
    )
);

CREATE TABLE reliability.exposure_scope_assignment (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    public_id uuid NOT NULL DEFAULT public.uuid_v7() UNIQUE,
    campaign_id bigint NOT NULL REFERENCES test.test_campaign(id),
    assessment_id bigint NOT NULL REFERENCES reliability.exposure_assessment(id) ON DELETE CASCADE,
    scope_id bigint NOT NULL REFERENCES reliability.mtbf_scope(id),
    inclusion_status text NOT NULL CHECK (inclusion_status IN ('INCLUDED', 'EXCLUDED', 'PENDING')),
    rule_id bigint REFERENCES reliability.mtbf_scope_rule(id),
    rationale text NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (assessment_id, scope_id)
);

CREATE TABLE reliability.interruption (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    public_id uuid NOT NULL DEFAULT public.uuid_v7() UNIQUE,
    campaign_id bigint NOT NULL REFERENCES test.test_campaign(id),
    asset_id bigint NOT NULL REFERENCES test.asset(id),
    event_id bigint NOT NULL UNIQUE REFERENCES test.test_event(id),
    classification text NOT NULL CHECK (classification IN ('FAILED', 'BLOCKED', 'NON_RELEVANT')),
    review_status text NOT NULL CHECK (review_status IN ('PENDING', 'CONFIRMED', 'RECLASSIFIED')),
    primary_error_code_id bigint REFERENCES catalog.error_code(id),
    source_raw_error text NOT NULL DEFAULT '',
    started_at timestamptz NOT NULL,
    recovered_at timestamptz,
    relevance_reason text NOT NULL DEFAULT '',
    confirmed_by bigint REFERENCES iam.app_user(id),
    confirmed_at timestamptz,
    voided boolean NOT NULL DEFAULT false,
    void_reason text NOT NULL DEFAULT '',
    voided_by bigint REFERENCES iam.app_user(id),
    voided_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK (recovered_at IS NULL OR recovered_at >= started_at),
    CHECK (classification <> 'FAILED' OR primary_error_code_id IS NOT NULL),
    CHECK (
        classification <> 'BLOCKED' OR
        primary_error_code_id IS NOT NULL OR
        btrim(source_raw_error) <> ''
    ),
    CHECK (
        classification <> 'NON_RELEVANT' OR
        btrim(relevance_reason) <> ''
    ),
    CHECK (NOT voided OR (voided_at IS NOT NULL AND btrim(void_reason) <> ''))
);

CREATE TABLE reliability.interruption_error_code (
    interruption_id bigint NOT NULL REFERENCES reliability.interruption(id) ON DELETE CASCADE,
    error_code_id bigint NOT NULL REFERENCES catalog.error_code(id),
    role text NOT NULL CHECK (role IN ('PRIMARY', 'ACCOMPANYING')),
    source_raw_code text NOT NULL DEFAULT '',
    PRIMARY KEY (interruption_id, error_code_id)
);

CREATE TABLE reliability.interruption_scope_assignment (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    interruption_id bigint NOT NULL REFERENCES reliability.interruption(id) ON DELETE CASCADE,
    scope_id bigint NOT NULL REFERENCES reliability.mtbf_scope(id),
    status text NOT NULL CHECK (status IN ('INCLUDED', 'EXCLUDED', 'PENDING')),
    rationale text NOT NULL DEFAULT '',
    assigned_by bigint REFERENCES iam.app_user(id),
    assigned_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (interruption_id, scope_id)
);

CREATE TABLE reliability.statistics_method (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    public_id uuid NOT NULL DEFAULT public.uuid_v7() UNIQUE,
    method_code text NOT NULL,
    version text NOT NULL,
    name text NOT NULL,
    asset_kind text CHECK (asset_kind IS NULL OR asset_kind IN ('WHOLE_MACHINE', 'MODULE')),
    parameter_schema jsonb NOT NULL DEFAULT '{}'::jsonb,
    default_parameters jsonb NOT NULL DEFAULT '{}'::jsonb,
    implementation_key text NOT NULL,
    status text NOT NULL CHECK (status IN ('DRAFT', 'PUBLISHED', 'RETIRED')),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (method_code, version)
);

CREATE TABLE reliability.calculation_run (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    public_id uuid NOT NULL DEFAULT public.uuid_v7() UNIQUE,
    population_id bigint NOT NULL REFERENCES reliability.mtbf_population(id),
    scope_id bigint NOT NULL REFERENCES reliability.mtbf_scope(id),
    method_id bigint NOT NULL REFERENCES reliability.statistics_method(id),
    status text NOT NULL CHECK (status IN ('QUEUED', 'RUNNING', 'SUCCEEDED', 'FAILED')),
    data_cutoff_at timestamptz NOT NULL,
    input_hash text NOT NULL,
    input_summary jsonb NOT NULL DEFAULT '{}'::jsonb,
    method_parameters jsonb NOT NULL DEFAULT '{}'::jsonb,
    output_summary jsonb NOT NULL DEFAULT '{}'::jsonb,
    evidence_summary jsonb NOT NULL DEFAULT '{}'::jsonb,
    initiated_by bigint REFERENCES iam.app_user(id),
    started_at timestamptz,
    completed_at timestamptz,
    error_detail text NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL DEFAULT now(),
    CHECK (completed_at IS NULL OR started_at IS NULL OR completed_at >= started_at)
);

CREATE TABLE reliability.calculation_contributor_campaign (
    calculation_run_id bigint NOT NULL REFERENCES reliability.calculation_run(id) ON DELETE CASCADE,
    campaign_id bigint NOT NULL REFERENCES test.test_campaign(id),
    PRIMARY KEY (calculation_run_id, campaign_id)
);

CREATE TABLE reliability.current_mtbf_result (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    public_id uuid NOT NULL DEFAULT public.uuid_v7() UNIQUE,
    population_id bigint NOT NULL REFERENCES reliability.mtbf_population(id),
    scope_id bigint NOT NULL REFERENCES reliability.mtbf_scope(id),
    calculation_run_id bigint NOT NULL REFERENCES reliability.calculation_run(id),
    exposure_seconds numeric(24,3) NOT NULL CHECK (exposure_seconds >= 0),
    relevant_failure_count integer NOT NULL CHECK (relevant_failure_count >= 0),
    pending_block_count integer NOT NULL CHECK (pending_block_count >= 0),
    pending_exposure_seconds numeric(24,3) NOT NULL DEFAULT 0 CHECK (pending_exposure_seconds >= 0),
    point_estimate_hours numeric(24,6),
    no_failure_exposure_hours numeric(24,6),
    result_payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    data_cutoff_at timestamptz NOT NULL,
    calculated_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (population_id, scope_id),
    CHECK (
        (relevant_failure_count = 0 AND point_estimate_hours IS NULL) OR
        relevant_failure_count > 0
    )
);

CREATE TABLE reliability.current_conclusion (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    public_id uuid NOT NULL DEFAULT public.uuid_v7() UNIQUE,
    population_id bigint NOT NULL REFERENCES reliability.mtbf_population(id),
    scope_id bigint NOT NULL REFERENCES reliability.mtbf_scope(id),
    calculation_run_id bigint NOT NULL REFERENCES reliability.calculation_run(id),
    status text NOT NULL CHECK (status IN ('DRAFT', 'REVIEWED', 'PUBLISHED')),
    conclusion_payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    evidence_completeness jsonb NOT NULL DEFAULT '{}'::jsonb,
    reviewed_by bigint REFERENCES iam.app_user(id),
    reviewed_at timestamptz,
    published_by bigint REFERENCES iam.app_user(id),
    published_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (population_id, scope_id)
);

CREATE TABLE reliability.recompute_job (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    public_id uuid NOT NULL DEFAULT public.uuid_v7() UNIQUE,
    campaign_id bigint REFERENCES test.test_campaign(id),
    resource_kind text NOT NULL,
    resource_key text NOT NULL,
    reason text NOT NULL,
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    status text NOT NULL DEFAULT 'PENDING' CHECK (
        status IN ('PENDING', 'PROCESSING', 'SUCCEEDED', 'FAILED')
    ),
    attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    requested_at timestamptz NOT NULL DEFAULT now(),
    available_at timestamptz NOT NULL DEFAULT now(),
    locked_by text,
    locked_at timestamptz,
    completed_at timestamptz,
    last_error text NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE reliability.pending_item (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    public_id uuid NOT NULL DEFAULT public.uuid_v7() UNIQUE,
    campaign_id bigint REFERENCES test.test_campaign(id),
    asset_id bigint REFERENCES test.asset(id),
    item_type text NOT NULL,
    entity_type text NOT NULL,
    entity_public_id uuid,
    status text NOT NULL CHECK (status IN ('OPEN', 'IN_PROGRESS', 'RESOLVED', 'DISMISSED')),
    severity text NOT NULL CHECK (severity IN ('INFO', 'WARNING', 'BLOCKING')),
    title text NOT NULL,
    detail jsonb NOT NULL DEFAULT '{}'::jsonb,
    owner_id bigint REFERENCES iam.app_user(id),
    due_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- Health checks, standardized observations and reference cohorts
-- ---------------------------------------------------------------------------

CREATE TABLE health.health_check_run (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    public_id uuid NOT NULL DEFAULT public.uuid_v7() UNIQUE,
    campaign_id bigint NOT NULL REFERENCES test.test_campaign(id),
    asset_id bigint NOT NULL REFERENCES test.asset(id),
    cycle_id bigint NOT NULL REFERENCES test.test_cycle(id),
    segment_id bigint NOT NULL REFERENCES test.analysis_segment(id),
    policy_id bigint NOT NULL REFERENCES catalog.health_check_policy(id),
    scheduled_at timestamptz,
    started_at timestamptz,
    completed_at timestamptz,
    status text NOT NULL CHECK (
        status IN ('DUE', 'RUNNING', 'COMPLETE', 'PARTIAL', 'MISSED', 'INVALID')
    ),
    quality_status text NOT NULL CHECK (quality_status IN ('VALID', 'PARTIAL', 'INVALID', 'MISSING')),
    health_status text CHECK (
        health_status IS NULL OR
        health_status IN ('NORMAL', 'ATTENTION', 'ABNORMAL', 'INSUFFICIENT_EVIDENCE')
    ),
    evidence_summary jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK (completed_at IS NULL OR started_at IS NULL OR completed_at >= started_at)
);

CREATE TABLE health.health_trigger_progress (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    health_check_run_id bigint NOT NULL REFERENCES health.health_check_run(id) ON DELETE CASCADE,
    trigger_id bigint NOT NULL REFERENCES catalog.health_check_trigger(id),
    observed_value numeric(20,6),
    reached boolean NOT NULL DEFAULT false,
    observed_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (health_check_run_id, trigger_id)
);

CREATE TABLE health.metric_observation (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    public_id uuid NOT NULL DEFAULT public.uuid_v7() UNIQUE,
    campaign_id bigint NOT NULL REFERENCES test.test_campaign(id),
    metric_version_id bigint NOT NULL REFERENCES catalog.metric_version(id),
    asset_id bigint NOT NULL REFERENCES test.asset(id),
    cycle_id bigint NOT NULL REFERENCES test.test_cycle(id),
    segment_id bigint REFERENCES test.analysis_segment(id),
    execution_id bigint REFERENCES test.test_execution(id),
    stage_id bigint REFERENCES test.execution_stage(id),
    health_check_run_id bigint REFERENCES health.health_check_run(id),
    configuration_snapshot_id bigint REFERENCES test.configuration_snapshot(id),
    observed_at timestamptz NOT NULL,
    exposure_coordinates jsonb NOT NULL DEFAULT '{}'::jsonb,
    canonical_numeric_value numeric(30,10),
    canonical_text_value text,
    original_numeric_value numeric(30,10),
    original_text_value text,
    original_unit text,
    conversion_method text NOT NULL DEFAULT '',
    quality_status text NOT NULL CHECK (quality_status IN ('VALID', 'PARTIAL', 'INVALID', 'MISSING')),
    source_kind text NOT NULL CHECK (source_kind IN ('NATIVE', 'MANUAL', 'LEGACY_SUMMARY')),
    analysis_eligible boolean NOT NULL DEFAULT true,
    raw_data_reference jsonb NOT NULL DEFAULT '{}'::jsonb,
    calculation_run_key text NOT NULL DEFAULT '',
    voided boolean NOT NULL DEFAULT false,
    void_reason text NOT NULL DEFAULT '',
    voided_by bigint REFERENCES iam.app_user(id),
    voided_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK (
        canonical_numeric_value IS NOT NULL OR canonical_text_value IS NOT NULL OR
        quality_status IN ('MISSING', 'INVALID')
    ),
    CHECK (source_kind <> 'LEGACY_SUMMARY' OR NOT analysis_eligible),
    CHECK (NOT voided OR (voided_at IS NOT NULL AND btrim(void_reason) <> ''))
);

CREATE TABLE health.analysis_reference_point (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    public_id uuid NOT NULL DEFAULT public.uuid_v7() UNIQUE,
    campaign_id bigint NOT NULL REFERENCES test.test_campaign(id),
    segment_id bigint NOT NULL REFERENCES test.analysis_segment(id),
    metric_observation_id bigint NOT NULL REFERENCES health.metric_observation(id),
    reference_kind text NOT NULL CHECK (reference_kind IN ('FORMAL_BASELINE', 'OBSERVATIONAL_ANCHOR')),
    selected_by bigint REFERENCES iam.app_user(id),
    selected_at timestamptz NOT NULL DEFAULT now(),
    rationale text NOT NULL DEFAULT '',
    UNIQUE (segment_id, metric_observation_id)
);

CREATE TABLE health.reference_cohort (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    public_id uuid NOT NULL DEFAULT public.uuid_v7() UNIQUE,
    cohort_code text NOT NULL UNIQUE,
    name text NOT NULL,
    asset_kind text NOT NULL CHECK (asset_kind IN ('WHOLE_MACHINE', 'MODULE')),
    compatibility_rule jsonb NOT NULL,
    version integer NOT NULL DEFAULT 1 CHECK (version > 0),
    status text NOT NULL CHECK (status IN ('DRAFT', 'ACTIVE', 'RETIRED')),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE health.reference_cohort_campaign (
    cohort_id bigint NOT NULL REFERENCES health.reference_cohort(id) ON DELETE CASCADE,
    campaign_id bigint NOT NULL REFERENCES test.test_campaign(id) ON DELETE CASCADE,
    PRIMARY KEY (cohort_id, campaign_id)
);

CREATE TABLE health.reference_membership (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    public_id uuid NOT NULL DEFAULT public.uuid_v7() UNIQUE,
    cohort_id bigint NOT NULL REFERENCES health.reference_cohort(id),
    asset_id bigint NOT NULL REFERENCES test.asset(id),
    segment_id bigint REFERENCES test.analysis_segment(id),
    valid_from timestamptz NOT NULL,
    valid_to timestamptz,
    status text NOT NULL CHECK (status IN ('CANDIDATE', 'APPROVED', 'SUSPENDED', 'EXCLUDED')),
    reason text NOT NULL DEFAULT '',
    reviewed_by bigint REFERENCES iam.app_user(id),
    reviewed_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK (valid_to IS NULL OR valid_to >= valid_from)
);

-- ---------------------------------------------------------------------------
-- Integration, one-time import metadata, artifacts and retention
-- ---------------------------------------------------------------------------

CREATE TABLE integration.ingestion_receipt (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    public_id uuid NOT NULL DEFAULT public.uuid_v7() UNIQUE,
    source_id bigint NOT NULL REFERENCES integration.ingestion_source(id),
    idempotency_key text NOT NULL,
    sequence_no bigint,
    source_time timestamptz,
    received_at timestamptz NOT NULL DEFAULT now(),
    payload_hash text NOT NULL,
    entity_type text,
    entity_public_id uuid,
    status text NOT NULL CHECK (status IN ('ACCEPTED', 'DUPLICATE', 'REJECTED', 'PARTIAL')),
    error_detail text NOT NULL DEFAULT '',
    UNIQUE (source_id, idempotency_key)
);

CREATE TABLE integration.import_batch (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    public_id uuid NOT NULL DEFAULT public.uuid_v7() UNIQUE,
    batch_code text NOT NULL UNIQUE,
    source_system text NOT NULL,
    status text NOT NULL CHECK (status IN ('PLANNED', 'RUNNING', 'SUCCEEDED', 'FAILED', 'REVERSED')),
    started_at timestamptz,
    completed_at timestamptz,
    source_summary jsonb NOT NULL DEFAULT '{}'::jsonb,
    result_summary jsonb NOT NULL DEFAULT '{}'::jsonb,
    initiated_by bigint REFERENCES iam.app_user(id),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK (completed_at IS NULL OR started_at IS NULL OR completed_at >= started_at)
);

CREATE TABLE integration.legacy_id_map (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    import_batch_id bigint NOT NULL REFERENCES integration.import_batch(id),
    source_system text NOT NULL,
    entity_type text NOT NULL,
    legacy_id text NOT NULL,
    new_entity_type text NOT NULL,
    new_public_id uuid NOT NULL,
    mapping_status text NOT NULL CHECK (mapping_status IN ('AUTO', 'MANUAL', 'REJECTED')),
    notes text NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (source_system, entity_type, legacy_id)
);

CREATE TABLE integration.migration_issue (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    public_id uuid NOT NULL DEFAULT public.uuid_v7() UNIQUE,
    import_batch_id bigint NOT NULL REFERENCES integration.import_batch(id),
    entity_type text NOT NULL,
    legacy_id text NOT NULL,
    issue_type text NOT NULL,
    detail jsonb NOT NULL DEFAULT '{}'::jsonb,
    status text NOT NULL CHECK (status IN ('OPEN', 'RESOLVED', 'ACCEPTED_AS_PARTIAL')),
    resolved_by bigint REFERENCES iam.app_user(id),
    resolved_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE integration.artifact (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    public_id uuid NOT NULL DEFAULT public.uuid_v7() UNIQUE,
    campaign_id bigint REFERENCES test.test_campaign(id),
    asset_id bigint REFERENCES test.asset(id),
    cycle_id bigint REFERENCES test.test_cycle(id),
    execution_id bigint REFERENCES test.test_execution(id),
    event_id bigint REFERENCES test.test_event(id),
    kind text NOT NULL,
    availability_status text NOT NULL CHECK (
        availability_status IN ('AVAILABLE', 'ARCHIVED', 'UNAVAILABLE_LEGACY', 'MISSING')
    ),
    object_key text UNIQUE,
    source_location text NOT NULL DEFAULT '',
    file_name text NOT NULL,
    mime_type text NOT NULL,
    size_bytes bigint CHECK (size_bytes IS NULL OR size_bytes >= 0),
    sha256 text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK (availability_status <> 'AVAILABLE' OR object_key IS NOT NULL)
);

CREATE TABLE integration.retention_policy (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    public_id uuid NOT NULL DEFAULT public.uuid_v7() UNIQUE,
    policy_code text NOT NULL,
    version text NOT NULL,
    data_class text NOT NULL,
    retention_spec jsonb NOT NULL,
    status text NOT NULL CHECK (status IN ('DRAFT', 'PUBLISHED', 'RETIRED')),
    published_by bigint REFERENCES iam.app_user(id),
    published_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (policy_code, version)
);

CREATE TABLE integration.evidence_hold (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    public_id uuid NOT NULL DEFAULT public.uuid_v7() UNIQUE,
    entity_type text NOT NULL,
    entity_public_id uuid NOT NULL,
    reason text NOT NULL,
    active boolean NOT NULL DEFAULT true,
    placed_by bigint REFERENCES iam.app_user(id),
    placed_at timestamptz NOT NULL DEFAULT now(),
    released_by bigint REFERENCES iam.app_user(id),
    released_at timestamptz,
    CHECK (active OR released_at IS NOT NULL)
);

-- ---------------------------------------------------------------------------
-- Append-only audit ledger
-- ---------------------------------------------------------------------------

CREATE TABLE audit.change_log (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    public_id uuid NOT NULL DEFAULT public.uuid_v7() UNIQUE,
    occurred_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    actor_user_id bigint,
    database_role text NOT NULL,
    request_id text NOT NULL DEFAULT '',
    change_reason text NOT NULL DEFAULT '',
    operation text NOT NULL CHECK (operation IN ('INSERT', 'UPDATE', 'DELETE')),
    schema_name text NOT NULL,
    table_name text NOT NULL,
    row_identity text NOT NULL DEFAULT '',
    before_data jsonb,
    after_data jsonb,
    source_ip inet
);

-- ---------------------------------------------------------------------------
-- Query views
-- ---------------------------------------------------------------------------

CREATE VIEW test.asset_test_time_summary AS
SELECT
    a.id AS asset_id,
    a.public_id AS asset_public_id,
    a.asset_code,
    coalesce(sum(ri.active_seconds) FILTER (WHERE NOT ri.voided), 0)::numeric(24,3) AS total_test_seconds,
    (
        coalesce(sum(ri.active_seconds) FILTER (WHERE NOT ri.voided), 0) / 3600
    )::numeric(24,6) AS total_test_hours,
    coalesce(sum(ri.active_seconds) FILTER (
        WHERE NOT ri.voided AND ri.source_kind = 'LEGACY_REPORTED'
    ), 0)::numeric(24,3) AS internal_legacy_seconds,
    coalesce(sum(ri.active_seconds) FILTER (
        WHERE NOT ri.voided AND ri.source_kind <> 'LEGACY_REPORTED'
    ), 0)::numeric(24,3) AS internal_native_seconds
FROM test.asset a
LEFT JOIN test.runtime_interval ri ON ri.asset_id = a.id
GROUP BY a.id, a.public_id, a.asset_code;

CREATE VIEW reliability.mtbf_eligible_runtime AS
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
  AND ri.source_kind <> 'LEGACY_REPORTED'
  AND NOT ri.voided;

-- ---------------------------------------------------------------------------
-- Indexes: equality columns first, time/range columns second.
-- ---------------------------------------------------------------------------

CREATE INDEX idx_auth_session_user_expires ON iam.auth_session(user_id, expires_at DESC);
CREATE INDEX idx_user_campaign_campaign ON iam.user_campaign_access(campaign_id, access_level);

CREATE INDEX idx_lab_site ON test.lab(site_id);
CREATE INDEX idx_station_lab ON test.station(lab_id);
CREATE INDEX idx_asset_kind_batch_status ON test.asset(asset_kind, batch_id, lifecycle_status) WHERE NOT voided;
CREATE INDEX idx_asset_code_trgm ON test.asset USING gin(asset_code gin_trgm_ops);
CREATE INDEX idx_configuration_asset_time ON test.configuration_snapshot(asset_id, effective_from DESC);
CREATE INDEX idx_configuration_fingerprint ON test.configuration_snapshot(fingerprint);
CREATE INDEX idx_campaign_program_status ON test.test_campaign(program_id, status);
CREATE INDEX idx_campaign_asset_asset ON test.campaign_asset(asset_id, campaign_id);
CREATE INDEX idx_coverage_campaign_status ON test.campaign_coverage_item(campaign_id, status);
CREATE INDEX idx_cycle_campaign_asset_time ON test.test_cycle(campaign_id, asset_id, started_at DESC);
CREATE INDEX idx_segment_cycle_time ON test.analysis_segment(cycle_id, started_at);
CREATE INDEX idx_execution_campaign_asset_time ON test.test_execution(campaign_id, asset_id, normalized_started_at DESC);
CREATE INDEX idx_execution_cycle ON test.test_execution(cycle_id);
CREATE INDEX idx_execution_station_time ON test.test_execution(station_id, normalized_started_at DESC);
CREATE INDEX idx_stage_execution_time ON test.execution_stage(execution_id, normalized_started_at);
CREATE INDEX idx_stage_mission_tag_tag ON test.stage_mission_tag(mission_tag_id, stage_id);
CREATE INDEX idx_runtime_campaign_asset_time ON test.runtime_interval(campaign_id, asset_id, started_at DESC) WHERE NOT voided;
CREATE INDEX idx_runtime_source_key ON test.runtime_interval(source_id, source_record_key) WHERE source_record_key IS NOT NULL;
CREATE UNIQUE INDEX uq_execution_source_key
    ON test.test_execution(source_id, source_execution_key)
    WHERE source_id IS NOT NULL AND source_execution_key IS NOT NULL;
CREATE UNIQUE INDEX uq_runtime_source_key
    ON test.runtime_interval(source_id, source_record_key)
    WHERE source_id IS NOT NULL AND source_record_key IS NOT NULL;
CREATE UNIQUE INDEX uq_event_source_key
    ON test.test_event(source_id, source_event_key)
    WHERE source_id IS NOT NULL AND source_event_key IS NOT NULL;
CREATE INDEX idx_event_campaign_asset_time ON test.test_event(campaign_id, asset_id, normalized_time DESC) WHERE NOT voided;
CREATE INDEX idx_event_type_time ON test.test_event(event_type, normalized_time DESC) WHERE NOT voided;
CREATE INDEX idx_event_payload_gin ON test.test_event USING gin(payload jsonb_path_ops);

CREATE INDEX idx_test_case_kind_enabled ON catalog.test_case(asset_kind, enabled);
CREATE INDEX idx_error_code_lookup ON catalog.error_code(catalog_version_id, unified_code) WHERE enabled;
CREATE INDEX idx_error_code_title_trgm ON catalog.error_code USING gin(title gin_trgm_ops);
CREATE INDEX idx_metric_aliases_gin ON catalog.metric_definition USING gin(aliases);
CREATE INDEX idx_health_trigger_policy ON catalog.health_check_trigger(policy_id, trigger_type);

CREATE INDEX idx_population_kind_status ON reliability.mtbf_population(asset_kind, status);
CREATE INDEX idx_population_campaign_campaign ON reliability.population_campaign(campaign_id, population_id);
CREATE INDEX idx_population_membership_lookup ON reliability.population_membership(population_id, status, valid_from DESC);
CREATE INDEX idx_equivalence_campaign_status ON reliability.configuration_equivalence_review(campaign_id, status, submitted_at);
CREATE INDEX idx_scope_kind_status ON reliability.mtbf_scope(asset_kind, status, scope_code);
CREATE INDEX idx_scope_rule_scope_order ON reliability.mtbf_scope_rule(scope_id, rule_order);
CREATE INDEX idx_exposure_campaign_asset ON reliability.exposure_assessment(campaign_id, asset_id, assignment_status, eligible);
CREATE INDEX idx_exposure_scope_lookup ON reliability.exposure_scope_assignment(scope_id, inclusion_status, assessment_id);
CREATE INDEX idx_interruption_campaign_class_time ON reliability.interruption(campaign_id, classification, started_at DESC) WHERE NOT voided;
CREATE INDEX idx_interruption_asset_time ON reliability.interruption(asset_id, started_at DESC) WHERE NOT voided;
CREATE INDEX idx_interruption_scope_status ON reliability.interruption_scope_assignment(scope_id, status);
CREATE INDEX idx_calculation_lookup ON reliability.calculation_run(population_id, scope_id, created_at DESC);
CREATE INDEX idx_calculation_campaign ON reliability.calculation_contributor_campaign(campaign_id, calculation_run_id);
CREATE INDEX idx_recompute_claim ON reliability.recompute_job(status, available_at, requested_at) WHERE status = 'PENDING';
CREATE INDEX idx_pending_campaign_status ON reliability.pending_item(campaign_id, status, severity, due_at);

CREATE INDEX idx_health_run_campaign_asset ON health.health_check_run(campaign_id, asset_id, status, scheduled_at);
CREATE INDEX idx_observation_asset_metric_time ON health.metric_observation(asset_id, metric_version_id, observed_at DESC) WHERE NOT voided;
CREATE INDEX idx_observation_campaign_time ON health.metric_observation(campaign_id, observed_at DESC) WHERE NOT voided;
CREATE INDEX idx_reference_membership_lookup ON health.reference_membership(cohort_id, status, valid_from DESC);
CREATE INDEX idx_reference_campaign_campaign ON health.reference_cohort_campaign(campaign_id, cohort_id);

CREATE INDEX idx_receipt_source_time ON integration.ingestion_receipt(source_id, received_at DESC);
CREATE INDEX idx_import_status ON integration.import_batch(status, created_at DESC);
CREATE INDEX idx_legacy_lookup ON integration.legacy_id_map(source_system, entity_type, legacy_id);
CREATE INDEX idx_migration_issue_status ON integration.migration_issue(import_batch_id, status, issue_type);
CREATE INDEX idx_artifact_campaign_entity ON integration.artifact(campaign_id, asset_id, execution_id);
CREATE INDEX idx_evidence_hold_entity ON integration.evidence_hold(entity_type, entity_public_id) WHERE active;

CREATE INDEX idx_audit_entity_time ON audit.change_log(schema_name, table_name, row_identity, occurred_at DESC);
CREATE INDEX idx_audit_actor_time ON audit.change_log(actor_user_id, occurred_at DESC);
CREATE INDEX idx_audit_request ON audit.change_log(request_id) WHERE request_id <> '';
