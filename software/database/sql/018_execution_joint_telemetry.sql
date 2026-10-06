CREATE TABLE catalog.telemetry_subject (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    public_id uuid NOT NULL DEFAULT public.uuid_v7() UNIQUE,
    subject_code text NOT NULL UNIQUE,
    name text NOT NULL,
    subject_kind text NOT NULL CHECK (
        subject_kind IN ('JOINT', 'BATTERY_CHANNEL', 'FRAME_POINT')
    ),
    target_part_code text NOT NULL
        REFERENCES catalog.test_target_part(part_code),
    display_order integer NOT NULL CHECK (display_order > 0),
    enabled boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK (subject_code ~ '^[A-Z0-9]+(-[A-Z0-9]+)*$'),
    UNIQUE (target_part_code, display_order)
);

CREATE INDEX ix_telemetry_subject_part_enabled
    ON catalog.telemetry_subject(target_part_code, display_order)
    WHERE enabled;

INSERT INTO catalog.telemetry_subject(
    subject_code, name, subject_kind, target_part_code, display_order
)
VALUES
    ('SYS-HIP-PITCH-L', '整机左髋俯仰关节', 'JOINT', 'SYS', 1),
    ('SYS-KNEE-PITCH-L', '整机左膝俯仰关节', 'JOINT', 'SYS', 2),
    ('SYS-SHOULDER-PITCH-R', '整机右肩俯仰关节', 'JOINT', 'SYS', 3),
    ('SARM-SHOULDER-PITCH', '单臂肩部俯仰关节', 'JOINT', 'SARM', 1),
    ('SARM-ELBOW-PITCH', '单臂肘部俯仰关节', 'JOINT', 'SARM', 2),
    ('SARM-WRIST-ROLL', '单臂腕部滚转关节', 'JOINT', 'SARM', 3),
    ('SLEG-HIP-PITCH', '单腿髋部俯仰关节', 'JOINT', 'SLEG', 1),
    ('SLEG-KNEE-PITCH', '单腿膝部俯仰关节', 'JOINT', 'SLEG', 2),
    ('SLEG-ANKLE-PITCH', '单腿踝部俯仰关节', 'JOINT', 'SLEG', 3),
    ('UPPER-SHOULDER-L', '上肢左肩联动关节', 'JOINT', 'UPPER', 1),
    ('UPPER-SHOULDER-R', '上肢右肩联动关节', 'JOINT', 'UPPER', 2),
    ('UPPER-WAIST-YAW', '上肢腰部偏航关节', 'JOINT', 'UPPER', 3),
    ('LOWER-HIP-L', '下肢左髋联动关节', 'JOINT', 'LOWER', 1),
    ('LOWER-HIP-R', '下肢右髋联动关节', 'JOINT', 'LOWER', 2),
    ('LOWER-WAIST-PITCH', '下肢腰部俯仰关节', 'JOINT', 'LOWER', 3),
    ('CHEST-BRACE-ACTUATOR', '胸腔支撑执行关节', 'JOINT', 'CHEST', 1),
    ('CHEST-FRAME-L', '胸腔左侧框架测点', 'FRAME_POINT', 'CHEST', 2),
    ('CHEST-FRAME-R', '胸腔右侧框架测点', 'FRAME_POINT', 'CHEST', 3),
    ('HEAD-YAW', '头部偏航关节', 'JOINT', 'HEAD', 1),
    ('HEAD-PITCH', '头部俯仰关节', 'JOINT', 'HEAD', 2),
    ('HEAD-ROLL', '头部滚转关节', 'JOINT', 'HEAD', 3),
    ('BAT-CONTACTOR-ACTUATOR', '电池接触器执行机构', 'JOINT', 'BAT', 1),
    ('BAT-PACK-BUS', '电池包母线测点', 'BATTERY_CHANNEL', 'BAT', 2),
    ('BAT-CELL-GROUP-A', '电芯组 A 测点', 'BATTERY_CHANNEL', 'BAT', 3)
ON CONFLICT (subject_code) DO UPDATE SET
    name = EXCLUDED.name,
    subject_kind = EXCLUDED.subject_kind,
    target_part_code = EXCLUDED.target_part_code,
    display_order = EXCLUDED.display_order,
    enabled = true,
    updated_at = clock_timestamp();

DO $$
DECLARE
    v_metric record;
    v_definition_id bigint;
BEGIN
    FOR v_metric IN
        SELECT *
        FROM (
            VALUES
                (
                    'JOINT-TARGET-POSITION', '关节目标位置', 'deg',
                    'DESCRIPTIVE', 'Target position command at a telemetry subject.'
                ),
                (
                    'JOINT-ACTUAL-POSITION', '关节实际位置', 'deg',
                    'DESCRIPTIVE', 'Measured position at a telemetry subject.'
                ),
                (
                    'JOINT-TRACKING-ERROR', '关节跟踪误差', 'deg',
                    'LOWER_BETTER', 'Actual minus target position.'
                ),
                (
                    'JOINT-VELOCITY', '关节速度', 'deg/s',
                    'DESCRIPTIVE', 'Measured angular velocity.'
                ),
                (
                    'JOINT-TORQUE', '关节力矩', 'N·m',
                    'DESCRIPTIVE', 'Measured or estimated joint torque.'
                ),
                (
                    'JOINT-CURRENT', '关节电流', 'A',
                    'LOWER_BETTER', 'Measured actuator or battery-channel current.'
                ),
                (
                    'JOINT-TEMPERATURE', '关节温度', '°C',
                    'LOWER_BETTER', 'Measured actuator, frame, or battery temperature.'
                ),
                (
                    'JOINT-VIBRATION-RMS', '关节振动 RMS', 'mm/s',
                    'LOWER_BETTER', 'Downsampled vibration velocity RMS.'
                )
        ) AS metrics(metric_code, canonical_name, canonical_unit, direction, description)
    LOOP
        INSERT INTO catalog.metric_definition(
            metric_code, canonical_name, aliases, domain, enabled
        )
        VALUES (
            v_metric.metric_code,
            v_metric.canonical_name,
            ARRAY[v_metric.description],
            'EXECUTION_TELEMETRY',
            true
        )
        ON CONFLICT (metric_code) DO UPDATE SET
            canonical_name = EXCLUDED.canonical_name,
            aliases = EXCLUDED.aliases,
            domain = EXCLUDED.domain,
            enabled = true,
            updated_at = clock_timestamp()
        RETURNING id INTO v_definition_id;

        INSERT INTO catalog.metric_version(
            metric_definition_id,
            version,
            canonical_unit,
            value_type,
            calculation_key,
            calculation_spec,
            aggregation_spec,
            conversion_spec,
            direction,
            comparability_spec,
            formal_eligible,
            status
        )
        VALUES (
            v_definition_id,
            '1.0.0',
            v_metric.canonical_unit,
            'NUMBER',
            'telemetry.direct_observation.v1',
            jsonb_build_object(
                'description', v_metric.description,
                'pointStorage', 'health.metric_observation'
            ),
            '{"seriesAggregation":"none","preserveSamples":true}'::jsonb,
            '{"method":"identity"}'::jsonb,
            v_metric.direction,
            '{"requiresSameMetricVersion":true,"requiresSameSubject":true}'::jsonb,
            true,
            'PUBLISHED'
        )
        ON CONFLICT (metric_definition_id, version) DO UPDATE SET
            canonical_unit = EXCLUDED.canonical_unit,
            value_type = EXCLUDED.value_type,
            calculation_key = EXCLUDED.calculation_key,
            calculation_spec = EXCLUDED.calculation_spec,
            aggregation_spec = EXCLUDED.aggregation_spec,
            conversion_spec = EXCLUDED.conversion_spec,
            direction = EXCLUDED.direction,
            comparability_spec = EXCLUDED.comparability_spec,
            formal_eligible = true,
            status = 'PUBLISHED',
            updated_at = clock_timestamp();
    END LOOP;
END
$$;

CREATE TABLE health.metric_series (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    public_id uuid NOT NULL DEFAULT public.uuid_v7() UNIQUE,
    campaign_id bigint NOT NULL REFERENCES test.test_campaign(id),
    asset_id bigint NOT NULL REFERENCES test.asset(id),
    cycle_id bigint NOT NULL REFERENCES test.test_cycle(id),
    segment_id bigint NOT NULL REFERENCES test.analysis_segment(id),
    execution_id bigint NOT NULL REFERENCES test.test_execution(id),
    stage_id bigint NOT NULL REFERENCES test.execution_stage(id),
    configuration_snapshot_id bigint NOT NULL
        REFERENCES test.configuration_snapshot(id),
    metric_version_id bigint NOT NULL REFERENCES catalog.metric_version(id),
    subject_code text NOT NULL
        REFERENCES catalog.telemetry_subject(subject_code),
    series_kind text NOT NULL CHECK (
        series_kind IN (
            'MOTION_CYCLE',
            'STEADY_STATE_WINDOW',
            'TRANSIENT',
            'DIAGNOSTIC'
        )
    ),
    cycle_index integer,
    started_at timestamptz NOT NULL,
    ended_at timestamptz NOT NULL,
    point_count integer NOT NULL DEFAULT 0 CHECK (point_count >= 0),
    sampling_interval_ms numeric(12,3) NOT NULL CHECK (sampling_interval_ms > 0),
    downsample_method text NOT NULL,
    quality_status text NOT NULL CHECK (
        quality_status IN ('VALID', 'PARTIAL', 'INVALID', 'MISSING')
    ),
    raw_data_reference jsonb NOT NULL DEFAULT '{}'::jsonb,
    source_id bigint REFERENCES integration.ingestion_source(id),
    source_series_key text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK (ended_at >= started_at),
    CHECK (btrim(downsample_method) <> ''),
    CHECK (
        (series_kind = 'MOTION_CYCLE' AND cycle_index IS NOT NULL AND cycle_index > 0)
        OR (series_kind <> 'MOTION_CYCLE' AND cycle_index IS NULL)
    ),
    CHECK ((source_id IS NULL) = (source_series_key IS NULL)),
    CHECK (
        source_series_key IS NULL OR btrim(source_series_key) <> ''
    ),
    CHECK (jsonb_typeof(raw_data_reference) = 'object'),
    CHECK (
        NOT (raw_data_reference ?| ARRAY['points', 'samples', 'values'])
    )
);

CREATE UNIQUE INDEX uq_metric_series_source_key
    ON health.metric_series(source_id, source_series_key)
    WHERE source_id IS NOT NULL AND source_series_key IS NOT NULL;

CREATE INDEX ix_metric_series_execution_subject
    ON health.metric_series(
        asset_id,
        execution_id,
        stage_id,
        metric_version_id,
        subject_code,
        started_at DESC
    );

CREATE INDEX ix_metric_series_campaign_time
    ON health.metric_series(campaign_id, started_at DESC, id);

ALTER TABLE health.metric_observation
    ADD COLUMN series_id bigint REFERENCES health.metric_series(id),
    ADD COLUMN sample_index integer;

ALTER TABLE health.metric_observation
    ADD CONSTRAINT ck_metric_observation_series_sample_pair
    CHECK ((series_id IS NULL) = (sample_index IS NULL)),
    ADD CONSTRAINT ck_metric_observation_sample_index
    CHECK (sample_index IS NULL OR sample_index >= 0);

CREATE UNIQUE INDEX uq_metric_observation_series_sample
    ON health.metric_observation(series_id, sample_index)
    WHERE series_id IS NOT NULL;

CREATE INDEX ix_metric_observation_series_time
    ON health.metric_observation(series_id, observed_at, sample_index)
    WHERE series_id IS NOT NULL AND NOT voided;

CREATE OR REPLACE FUNCTION health.validate_metric_series_links()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, catalog, test, health
AS $$
DECLARE
    v_execution_campaign bigint;
    v_execution_asset bigint;
    v_execution_cycle bigint;
    v_execution_config bigint;
    v_stage_campaign bigint;
    v_stage_execution bigint;
    v_stage_started timestamptz;
    v_stage_ended timestamptz;
    v_segment_campaign bigint;
    v_segment_cycle bigint;
    v_segment_config bigint;
    v_asset_kind text;
    v_asset_target text;
    v_subject_kind text;
    v_subject_target text;
    v_metric_code text;
    v_metric_status text;
    v_metric_value_type text;
    v_metric_enabled boolean;
BEGIN
    IF TG_OP = 'UPDATE'
       AND EXISTS (
           SELECT 1
           FROM health.metric_observation observation
           WHERE observation.series_id = OLD.id
       )
       AND ROW(
           NEW.campaign_id,
           NEW.asset_id,
           NEW.cycle_id,
           NEW.segment_id,
           NEW.execution_id,
           NEW.stage_id,
           NEW.configuration_snapshot_id,
           NEW.metric_version_id,
           NEW.subject_code,
           NEW.series_kind,
           NEW.cycle_index
       ) IS DISTINCT FROM ROW(
           OLD.campaign_id,
           OLD.asset_id,
           OLD.cycle_id,
           OLD.segment_id,
           OLD.execution_id,
           OLD.stage_id,
           OLD.configuration_snapshot_id,
           OLD.metric_version_id,
           OLD.subject_code,
           OLD.series_kind,
           OLD.cycle_index
       )
    THEN
        RAISE EXCEPTION
            'metric series % with observations cannot be rebound', OLD.id;
    END IF;

    IF TG_OP = 'UPDATE'
       AND EXISTS (
           SELECT 1
           FROM health.metric_observation observation
           WHERE observation.series_id = OLD.id
             AND (
                 observation.observed_at < NEW.started_at
                 OR observation.observed_at > NEW.ended_at
             )
       )
    THEN
        RAISE EXCEPTION
            'metric series % interval would exclude existing observations',
            OLD.id;
    END IF;

    SELECT
        execution.campaign_id,
        execution.asset_id,
        execution.cycle_id,
        execution.configuration_snapshot_id
    INTO
        v_execution_campaign,
        v_execution_asset,
        v_execution_cycle,
        v_execution_config
    FROM test.test_execution execution
    WHERE execution.id = NEW.execution_id;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'metric series execution % does not exist', NEW.execution_id;
    END IF;

    IF ROW(
        v_execution_campaign,
        v_execution_asset,
        v_execution_cycle,
        v_execution_config
    ) IS DISTINCT FROM ROW(
        NEW.campaign_id,
        NEW.asset_id,
        NEW.cycle_id,
        NEW.configuration_snapshot_id
    ) THEN
        RAISE EXCEPTION
            'metric series execution, campaign, asset, cycle and configuration do not agree';
    END IF;

    SELECT
        stage.campaign_id,
        stage.execution_id,
        stage.normalized_started_at,
        stage.normalized_ended_at
    INTO
        v_stage_campaign,
        v_stage_execution,
        v_stage_started,
        v_stage_ended
    FROM test.execution_stage stage
    WHERE stage.id = NEW.stage_id;

    IF NOT FOUND
       OR v_stage_campaign IS DISTINCT FROM NEW.campaign_id
       OR v_stage_execution IS DISTINCT FROM NEW.execution_id
    THEN
        RAISE EXCEPTION
            'metric series stage does not belong to its campaign and execution';
    END IF;

    IF v_stage_started IS NOT NULL AND NEW.started_at < v_stage_started THEN
        RAISE EXCEPTION 'metric series starts before its execution stage';
    END IF;
    IF v_stage_ended IS NOT NULL AND NEW.ended_at > v_stage_ended THEN
        RAISE EXCEPTION 'metric series ends after its execution stage';
    END IF;

    SELECT
        segment.campaign_id,
        segment.cycle_id,
        segment.configuration_snapshot_id
    INTO
        v_segment_campaign,
        v_segment_cycle,
        v_segment_config
    FROM test.analysis_segment segment
    WHERE segment.id = NEW.segment_id;

    IF NOT FOUND
       OR ROW(v_segment_campaign, v_segment_cycle, v_segment_config)
          IS DISTINCT FROM ROW(
              NEW.campaign_id,
              NEW.cycle_id,
              NEW.configuration_snapshot_id
          )
    THEN
        RAISE EXCEPTION
            'metric series segment does not match campaign, cycle and configuration';
    END IF;

    SELECT
        asset.asset_kind,
        CASE
            WHEN asset.asset_kind = 'WHOLE_MACHINE' THEN 'SYS'
            ELSE profile.target_part_code
        END
    INTO v_asset_kind, v_asset_target
    FROM test.asset asset
    LEFT JOIN test.module_profile profile ON profile.asset_id = asset.id
    WHERE asset.id = NEW.asset_id;

    SELECT subject.subject_kind, subject.target_part_code
    INTO v_subject_kind, v_subject_target
    FROM catalog.telemetry_subject subject
    WHERE subject.subject_code = NEW.subject_code
      AND subject.enabled;

    IF NOT FOUND OR v_subject_target IS DISTINCT FROM v_asset_target THEN
        RAISE EXCEPTION
            'telemetry subject % does not match asset target part %',
            NEW.subject_code,
            v_asset_target;
    END IF;

    SELECT
        definition.metric_code,
        version.status,
        version.value_type,
        definition.enabled
    INTO
        v_metric_code,
        v_metric_status,
        v_metric_value_type,
        v_metric_enabled
    FROM catalog.metric_version version
    JOIN catalog.metric_definition definition
      ON definition.id = version.metric_definition_id
    WHERE version.id = NEW.metric_version_id;

    IF NOT FOUND
       OR v_metric_status IS DISTINCT FROM 'PUBLISHED'
       OR v_metric_value_type IS DISTINCT FROM 'NUMBER'
       OR NOT v_metric_enabled
    THEN
        RAISE EXCEPTION
            'metric series requires an enabled published numeric metric version';
    END IF;

    IF v_subject_kind = 'BATTERY_CHANNEL'
       AND v_metric_code NOT IN (
           'JOINT-CURRENT',
           'JOINT-TEMPERATURE',
           'JOINT-VIBRATION-RMS'
       )
    THEN
        RAISE EXCEPTION
            'metric % is not valid for battery telemetry subject %',
            v_metric_code,
            NEW.subject_code;
    END IF;

    IF v_subject_kind = 'FRAME_POINT'
       AND v_metric_code NOT IN (
           'JOINT-TORQUE',
           'JOINT-TEMPERATURE',
           'JOINT-VIBRATION-RMS'
       )
    THEN
        RAISE EXCEPTION
            'metric % is not valid for frame telemetry subject %',
            v_metric_code,
            NEW.subject_code;
    END IF;

    RETURN NEW;
END
$$;

CREATE OR REPLACE FUNCTION health.validate_metric_observation_series()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, health
AS $$
DECLARE
    v_series health.metric_series%ROWTYPE;
BEGIN
    IF NEW.series_id IS NULL THEN
        RETURN NEW;
    END IF;

    SELECT *
    INTO v_series
    FROM health.metric_series series
    WHERE series.id = NEW.series_id;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'metric series % does not exist', NEW.series_id;
    END IF;

    IF ROW(
        NEW.campaign_id,
        NEW.asset_id,
        NEW.cycle_id,
        NEW.segment_id,
        NEW.execution_id,
        NEW.stage_id,
        NEW.configuration_snapshot_id,
        NEW.metric_version_id
    ) IS DISTINCT FROM ROW(
        v_series.campaign_id,
        v_series.asset_id,
        v_series.cycle_id,
        v_series.segment_id,
        v_series.execution_id,
        v_series.stage_id,
        v_series.configuration_snapshot_id,
        v_series.metric_version_id
    ) THEN
        RAISE EXCEPTION
            'metric observation does not match its series context';
    END IF;

    IF NEW.observed_at < v_series.started_at
       OR NEW.observed_at > v_series.ended_at
    THEN
        RAISE EXCEPTION
            'metric observation timestamp is outside its series interval';
    END IF;

    RETURN NEW;
END
$$;

REVOKE ALL ON FUNCTION health.validate_metric_series_links() FROM PUBLIC;
REVOKE ALL ON FUNCTION health.validate_metric_observation_series() FROM PUBLIC;

CREATE TRIGGER trg_validate_metric_series_links
BEFORE INSERT OR UPDATE OF
    campaign_id,
    asset_id,
    cycle_id,
    segment_id,
    execution_id,
    stage_id,
    configuration_snapshot_id,
    metric_version_id,
    subject_code,
    series_kind,
    cycle_index,
    started_at,
    ended_at
ON health.metric_series
FOR EACH ROW
EXECUTE FUNCTION health.validate_metric_series_links();

CREATE TRIGGER trg_validate_metric_observation_series
BEFORE INSERT OR UPDATE OF
    campaign_id,
    metric_version_id,
    asset_id,
    cycle_id,
    segment_id,
    execution_id,
    stage_id,
    configuration_snapshot_id,
    observed_at,
    series_id,
    sample_index
ON health.metric_observation
FOR EACH ROW
EXECUTE FUNCTION health.validate_metric_observation_series();

CREATE TRIGGER trg_set_updated_at
BEFORE UPDATE ON catalog.telemetry_subject
FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();

CREATE TRIGGER trg_set_updated_at
BEFORE UPDATE ON health.metric_series
FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();

CREATE TRIGGER trg_audit_row_change
AFTER INSERT OR UPDATE OR DELETE ON catalog.telemetry_subject
FOR EACH ROW EXECUTE FUNCTION audit.capture_row_change();

CREATE TRIGGER trg_audit_row_change
AFTER INSERT OR UPDATE OR DELETE ON health.metric_series
FOR EACH ROW EXECUTE FUNCTION audit.capture_row_change();

CREATE TRIGGER trg_enqueue_recompute
AFTER INSERT OR UPDATE ON health.metric_series
FOR EACH ROW EXECUTE FUNCTION reliability.enqueue_recompute_from_change();

ALTER TABLE catalog.telemetry_subject ENABLE ROW LEVEL SECURITY;
CREATE POLICY authenticated_select ON catalog.telemetry_subject
    FOR SELECT USING (iam.is_authenticated());
CREATE POLICY admin_write ON catalog.telemetry_subject
    FOR ALL USING (iam.is_system_admin()) WITH CHECK (iam.is_system_admin());

ALTER TABLE health.metric_series ENABLE ROW LEVEL SECURITY;
CREATE POLICY campaign_scoped_select ON health.metric_series
    FOR SELECT USING (iam.can_access_campaign(campaign_id, false));
CREATE POLICY campaign_scoped_write ON health.metric_series
    FOR ALL
    USING (iam.can_access_campaign(campaign_id, true))
    WITH CHECK (iam.can_access_campaign(campaign_id, true));

GRANT SELECT, INSERT, UPDATE ON catalog.telemetry_subject TO __RP1_APP_USER__;
GRANT SELECT ON catalog.telemetry_subject TO __RP1_READONLY_USER__;
GRANT USAGE, SELECT ON SEQUENCE catalog.telemetry_subject_id_seq
TO __RP1_APP_USER__;

GRANT SELECT, INSERT, UPDATE ON health.metric_series TO __RP1_APP_USER__;
GRANT SELECT ON health.metric_series TO __RP1_READONLY_USER__;
GRANT USAGE, SELECT ON SEQUENCE health.metric_series_id_seq
TO __RP1_APP_USER__;

COMMENT ON TABLE catalog.telemetry_subject IS
    'Version-independent directory of joints, battery channels and frame telemetry points.';
COMMENT ON TABLE health.metric_series IS
    'Normalized telemetry series header; sample values remain relational metric_observation rows.';
COMMENT ON COLUMN health.metric_series.raw_data_reference IS
    'Reference metadata only. Point arrays are prohibited and belong in metric_observation rows.';
COMMENT ON COLUMN health.metric_observation.series_id IS
    'Optional normalized series header for sampled telemetry; NULL preserves legacy scalar observations.';
COMMENT ON COLUMN health.metric_observation.sample_index IS
    'Zero-based sample position, unique inside a normalized metric series.';
