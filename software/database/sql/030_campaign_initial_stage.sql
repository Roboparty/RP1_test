-- A new test plan starts with 测试阶段1. Stages are not yet assigned across plans.
-- Module cases that already have a -001 sample get one CLI plan context when that
-- program and station exist. Cases that already have a context are left alone.

CREATE TABLE IF NOT EXISTS test.campaign_stage (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    public_id uuid NOT NULL DEFAULT public.uuid_v7() UNIQUE,
    campaign_id bigint NOT NULL REFERENCES test.test_campaign(id) ON DELETE CASCADE,
    sequence_no integer NOT NULL CHECK (sequence_no > 0),
    stage_name text NOT NULL CHECK (btrim(stage_name) <> ''),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (campaign_id, sequence_no)
);

CREATE OR REPLACE FUNCTION test.ensure_initial_campaign_stage()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    INSERT INTO test.campaign_stage (campaign_id, sequence_no, stage_name)
    VALUES (NEW.id, 1, '测试阶段1')
    ON CONFLICT (campaign_id, sequence_no) DO NOTHING;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_campaign_initial_stage ON test.test_campaign;
CREATE TRIGGER trg_campaign_initial_stage
AFTER INSERT ON test.test_campaign
FOR EACH ROW EXECUTE FUNCTION test.ensure_initial_campaign_stage();

INSERT INTO test.campaign_stage (campaign_id, sequence_no, stage_name)
SELECT c.id, 1, '测试阶段1'
FROM test.test_campaign c
WHERE NOT EXISTS (
    SELECT 1 FROM test.campaign_stage s
    WHERE s.campaign_id = c.id AND s.sequence_no = 1
);

ALTER TABLE test.campaign_stage ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS campaign_scoped_select ON test.campaign_stage;
DROP POLICY IF EXISTS campaign_scoped_write ON test.campaign_stage;
CREATE POLICY campaign_scoped_select ON test.campaign_stage
    FOR SELECT USING (iam.can_access_campaign(campaign_id, false));
CREATE POLICY campaign_scoped_write ON test.campaign_stage
    FOR ALL USING (iam.can_access_campaign(campaign_id, true))
    WITH CHECK (iam.can_access_campaign(campaign_id, true));

DROP TRIGGER IF EXISTS trg_set_updated_at ON test.campaign_stage;
CREATE TRIGGER trg_set_updated_at
BEFORE UPDATE ON test.campaign_stage
FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();

DROP TRIGGER IF EXISTS trg_audit_row_change ON test.campaign_stage;
CREATE TRIGGER trg_audit_row_change
AFTER INSERT OR UPDATE OR DELETE ON test.campaign_stage
FOR EACH ROW EXECUTE FUNCTION audit.capture_row_change();

GRANT SELECT, INSERT, UPDATE, DELETE ON test.campaign_stage TO __RP1_APP_USER__;
GRANT SELECT ON test.campaign_stage TO __RP1_READONLY_USER__;
GRANT USAGE, SELECT ON SEQUENCE test.campaign_stage_id_seq TO __RP1_APP_USER__;

DO $$
DECLARE
    v_program bigint;
    v_campaign bigint;
    v_campaign_public uuid;
    v_station bigint;
    v_station_public uuid;
    v_case record;
    v_asset bigint;
    v_asset_public uuid;
    v_config bigint;
    v_config_public uuid;
    v_cycle bigint;
    v_cycle_public uuid;
    v_segment uuid;
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM test.asset
        WHERE asset_code = 'RP1.3-SARM-001' AND NOT voided
    ) THEN
        RETURN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM test.station WHERE station_code = 'RD-01' AND enabled) THEN
        RETURN;
    END IF;

    DELETE FROM test.campaign_coverage_item ci
    USING test.test_campaign c
    WHERE ci.campaign_id = c.id AND c.campaign_code = 'STATION-MODULE-20261014';
    DELETE FROM test.analysis_segment seg
    USING test.test_campaign c
    WHERE seg.campaign_id = c.id AND c.campaign_code = 'STATION-MODULE-20261014';
    DELETE FROM test.test_cycle cy
    USING test.test_campaign c
    WHERE cy.campaign_id = c.id AND c.campaign_code = 'STATION-MODULE-20261014';
    DELETE FROM test.campaign_asset ca
    USING test.test_campaign c
    WHERE ca.campaign_id = c.id AND c.campaign_code = 'STATION-MODULE-20261014';
    DELETE FROM test.campaign_stage st
    USING test.test_campaign c
    WHERE st.campaign_id = c.id AND c.campaign_code = 'STATION-MODULE-20261014';
    DELETE FROM test.test_campaign WHERE campaign_code = 'STATION-MODULE-20261014';
    DELETE FROM test.test_program p
    WHERE p.program_code = 'STATION-CONTEXT-20261014'
      AND NOT EXISTS (SELECT 1 FROM test.test_campaign c WHERE c.program_id = p.id);

    SELECT id INTO v_program
    FROM test.test_program WHERE program_code = 'LEGACY-EXECUTION-HISTORY-20260918';
    IF v_program IS NULL THEN
        RETURN;
    END IF;
    PERFORM set_config('app.change_reason', '工位用例上下文：用例编号加测试阶段1', true);
    PERFORM set_config('app.request_id', 'station-case-contexts-20261014', true);

    FOR v_case IN
        SELECT DISTINCT ON (tc.case_code)
            tc.case_code,
            tc.target_part_code,
            v.id AS version_id,
            v.public_id AS version_public
        FROM catalog.test_case tc
        JOIN catalog.test_case_version v
          ON v.test_case_id = tc.id AND v.status = 'PUBLISHED'
        WHERE tc.enabled
          AND tc.target_part_code IN ('SARM', 'SLEG', 'LOWER', 'UPPER')
        ORDER BY tc.case_code, v.published_at DESC NULLS LAST, v.id DESC
    LOOP
        SELECT a.id, a.public_id INTO v_asset, v_asset_public
        FROM test.asset a
        JOIN test.module_profile m ON m.asset_id = a.id
        WHERE NOT a.voided
          AND m.target_part_code = v_case.target_part_code
          AND a.asset_code = 'RP1.3-' || v_case.target_part_code || '-001';
        IF v_asset IS NULL THEN
            CONTINUE;
        END IF;
        SELECT c.id, c.public_id INTO v_config, v_config_public
        FROM test.configuration_snapshot c
        WHERE c.asset_id = v_asset
        ORDER BY c.effective_from DESC, c.id DESC
        LIMIT 1;
        IF v_config IS NULL THEN
            CONTINUE;
        END IF;
        IF EXISTS (
            SELECT 1
            FROM test.campaign_coverage_item ci
            JOIN test.test_campaign existing ON existing.id = ci.campaign_id
            WHERE ci.target_asset_id = v_asset
              AND ci.test_case_version_id = v_case.version_id
              AND ci.status <> 'CANCELLED'
              AND existing.status IN ('PLANNED', 'ACTIVE')
        ) THEN
            CONTINUE;
        END IF;

        SELECT id, public_id INTO v_station, v_station_public
        FROM test.station
        WHERE station_code = CASE v_case.target_part_code
            WHEN 'SARM' THEN 'RD-01'
            WHEN 'SLEG' THEN 'RD-02'
            ELSE 'RD-03'
        END AND enabled;
        IF v_station IS NULL THEN
            CONTINUE;
        END IF;

        INSERT INTO test.test_campaign (program_id, campaign_code, name, asset_kind, status)
        SELECT v_program, 'CLI-' || v_case.target_part_code || '-20261014',
               v_case.target_part_code || ' 代码执行', 'MODULE', 'PLANNED'
        WHERE NOT EXISTS (
            SELECT 1 FROM test.test_campaign
            WHERE program_id = v_program
              AND campaign_code = 'CLI-' || v_case.target_part_code || '-20261014'
        );
        SELECT id, public_id INTO v_campaign, v_campaign_public
        FROM test.test_campaign
        WHERE program_id = v_program
          AND campaign_code = 'CLI-' || v_case.target_part_code || '-20261014';
        IF v_campaign IS NULL THEN
            CONTINUE;
        END IF;
        UPDATE test.test_campaign SET status = 'ACTIVE'
        WHERE id = v_campaign AND status = 'PLANNED';

        INSERT INTO test.campaign_asset (campaign_id, asset_id)
        VALUES (v_campaign, v_asset)
        ON CONFLICT (campaign_id, asset_id) DO UPDATE SET left_at = NULL;

        INSERT INTO test.test_cycle (cycle_code, campaign_id, asset_id, status)
        VALUES ('CLI-20261014-' || v_case.case_code, v_campaign, v_asset, 'PLANNED')
        ON CONFLICT (cycle_code) DO NOTHING;
        SELECT id, public_id INTO v_cycle, v_cycle_public
        FROM test.test_cycle WHERE cycle_code = 'CLI-20261014-' || v_case.case_code;
        IF v_cycle IS NULL OR NOT EXISTS (
            SELECT 1 FROM test.test_cycle
            WHERE id = v_cycle AND campaign_id = v_campaign AND asset_id = v_asset
        ) THEN
            CONTINUE;
        END IF;

        IF NOT EXISTS (
            SELECT 1 FROM test.analysis_segment
            WHERE cycle_id = v_cycle AND segment_no = 1
        ) THEN
            INSERT INTO test.analysis_segment (
                campaign_id, cycle_id, configuration_snapshot_id, segment_no, reason, started_at
            ) VALUES (
                v_campaign, v_cycle, v_config, 1, 'ORIGINAL', clock_timestamp()
            )
            RETURNING public_id INTO v_segment;
        ELSE
            SELECT public_id INTO v_segment
            FROM test.analysis_segment
            WHERE cycle_id = v_cycle AND segment_no = 1;
        END IF;

        IF EXISTS (
            SELECT 1 FROM test.campaign_coverage_item
            WHERE campaign_id = v_campaign
              AND target_asset_id = v_asset
              AND test_case_version_id = v_case.version_id
              AND status <> 'CANCELLED'
        ) THEN
            CONTINUE;
        END IF;
        INSERT INTO test.campaign_coverage_item (
            campaign_id, test_case_version_id, target_asset_id,
            target_configuration_selector, planned_runs
        ) VALUES (
            v_campaign,
            v_case.version_id,
            v_asset,
            jsonb_build_object(
                'campaign_id', v_campaign_public,
                'cycle_id', v_cycle_public,
                'segment_id', v_segment,
                'asset_id', v_asset_public,
                'configuration_id', v_config_public,
                'test_case_version_id', v_case.version_public,
                'station_id', v_station_public
            ),
            1
        );
    END LOOP;
END;
$$;
