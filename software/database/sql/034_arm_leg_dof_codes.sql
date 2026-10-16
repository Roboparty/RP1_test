-- Full-degree samples and cases drop the S. ARM is a 7-DoF arm, LEG is a
-- 6-DoF leg. SARM and SLEG remain available for the reduced-degree variants.
-- Historical single-arm and single-leg records were the full-degree hardware.

SELECT set_config('app.change_reason', '以往单臂、单腿记录改为7自由度ARM与6自由度LEG，用例编号去掉S', true);

ALTER TABLE test.asset DROP CONSTRAINT ck_asset_code_allowed_part_segment;
ALTER TABLE test.asset
    ADD CONSTRAINT ck_asset_code_allowed_part_segment
    CHECK (
        split_part(asset_code, '-', 2)
        IN ('ARM', 'SARM', 'LEG', 'SLEG', 'SYS', 'UPPER', 'LOWER', 'CHEST', 'HEAD', 'BAT')
    );

ALTER TABLE catalog.test_target_part DROP CONSTRAINT ck_test_target_part_asset_code_segment;
ALTER TABLE catalog.test_target_part
    ADD CONSTRAINT ck_test_target_part_asset_code_segment
    CHECK (
        part_code IN ('ARM', 'SARM', 'LEG', 'SLEG', 'SYS', 'UPPER', 'LOWER', 'CHEST', 'HEAD', 'BAT')
        AND asset_code_segment = part_code
    );

INSERT INTO catalog.test_target_part(part_code, name, asset_kind, description, asset_code_segment, enabled)
VALUES
    ('ARM', '7自由度单臂', 'MODULE', '7自由度单臂模块', 'ARM', true),
    ('LEG', '6自由度单腿', 'MODULE', '6自由度单腿模块', 'LEG', true)
ON CONFLICT (part_code) DO UPDATE SET
    name = EXCLUDED.name,
    asset_kind = EXCLUDED.asset_kind,
    description = EXCLUDED.description,
    asset_code_segment = EXCLUDED.asset_code_segment,
    enabled = true,
    updated_at = clock_timestamp();

UPDATE catalog.test_target_part
SET name = '5自由度单臂',
    description = '5自由度单臂模块',
    updated_at = clock_timestamp()
WHERE part_code = 'SARM';

UPDATE catalog.test_target_part
SET name = '5自由度单腿',
    description = '5自由度单腿模块',
    updated_at = clock_timestamp()
WHERE part_code = 'SLEG';

ALTER TABLE test.test_execution DROP CONSTRAINT ck_execution_code_case_time_asset_station;
ALTER TABLE test.test_execution
    ADD CONSTRAINT ck_execution_code_case_time_asset_station
    CHECK (
        execution_code ~
        '^[A-Z0-9]+(-[A-Z0-9]+)*_[0-9]{10}_RP[0-9]+\.[0-9]+-(ARM|SARM|LEG|SLEG|SYS|UPPER|LOWER|CHEST|HEAD|BAT)-[0-9]{3}_[A-Z0-9]+(-[A-Z0-9]+)*(_E[0-9]+)?$'
    ) NOT VALID;

UPDATE catalog.test_case
SET case_code = replace(replace(replace(case_code, 'REL-SARM-', 'REL-ARM-'), 'REL-SLEG-', 'REL-LEG-'), 'PERF-SARM-', 'PERF-ARM-'),
    target_part_code = CASE target_part_code
        WHEN 'SARM' THEN 'ARM'
        WHEN 'SLEG' THEN 'LEG'
        ELSE target_part_code
    END,
    updated_at = clock_timestamp()
WHERE case_code LIKE '%SARM%' OR case_code LIKE '%SLEG%' OR target_part_code IN ('SARM', 'SLEG');

ALTER TABLE test.asset DISABLE TRIGGER trg_asset_code_target;
ALTER TABLE test.module_profile DISABLE TRIGGER trg_module_profile_target;

UPDATE test.module_profile
SET target_part_code = CASE target_part_code
        WHEN 'SARM' THEN 'ARM'
        WHEN 'SLEG' THEN 'LEG'
        ELSE target_part_code
    END,
    updated_at = clock_timestamp()
WHERE target_part_code IN ('SARM', 'SLEG');

UPDATE test.asset
SET asset_code = CASE
        WHEN asset_code LIKE 'RP1.3-SARM-%' THEN replace(asset_code, 'RP1.3-SARM-', 'RP1.3-ARM-')
        WHEN asset_code LIKE 'RP1.3-SLEG-%' THEN replace(asset_code, 'RP1.3-SLEG-', 'RP1.3-LEG-')
        ELSE asset_code
    END,
    display_name = CASE
        WHEN asset_code LIKE 'RP1.3-SARM-%' THEN '7自由度单臂 ' || right(asset_code, 3)
        WHEN asset_code LIKE 'RP1.3-SLEG-%' THEN '6自由度单腿 ' || right(asset_code, 3)
        ELSE display_name
    END,
    updated_at = clock_timestamp()
WHERE asset_code LIKE 'RP1.3-SARM-%' OR asset_code LIKE 'RP1.3-SLEG-%';

UPDATE test.configuration_snapshot
SET fingerprint = replace(replace(fingerprint, 'RP1.3-SARM-', 'RP1.3-ARM-'), 'RP1.3-SLEG-', 'RP1.3-LEG-'),
    updated_at = clock_timestamp()
WHERE fingerprint LIKE '%RP1.3-SARM-%' OR fingerprint LIKE '%RP1.3-SLEG-%';

ALTER TABLE test.asset ENABLE TRIGGER trg_asset_code_target;
ALTER TABLE test.module_profile ENABLE TRIGGER trg_module_profile_target;

UPDATE test.test_execution
SET execution_code = execution_code
WHERE execution_code LIKE '%SARM%' OR execution_code LIKE '%SLEG%';

ALTER TABLE test.test_execution VALIDATE CONSTRAINT ck_execution_code_case_time_asset_station;

CREATE OR REPLACE FUNCTION test.validate_execution_links()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    v_cycle_campaign bigint;
    v_cycle_asset bigint;
    v_config_asset bigint;
    v_campaign_kind text;
    v_asset_kind text;
    v_case_kind text;
    v_case_target text;
    v_asset_target text;
BEGIN
    SELECT campaign_id, asset_id INTO v_cycle_campaign, v_cycle_asset
    FROM test.test_cycle WHERE id = NEW.cycle_id;
    SELECT asset_id INTO v_config_asset
    FROM test.configuration_snapshot WHERE id = NEW.configuration_snapshot_id;
    SELECT c.asset_kind INTO v_campaign_kind
    FROM test.test_campaign c WHERE c.id = NEW.campaign_id;
    SELECT a.asset_kind INTO v_asset_kind
    FROM test.asset a WHERE a.id = NEW.asset_id;
    SELECT tc.asset_kind, tc.target_part_code INTO v_case_kind, v_case_target
    FROM catalog.test_case_version tcv
    JOIN catalog.test_case tc ON tc.id = tcv.test_case_id
    WHERE tcv.id = NEW.test_case_version_id;

    IF v_cycle_campaign IS DISTINCT FROM NEW.campaign_id OR v_cycle_asset IS DISTINCT FROM NEW.asset_id THEN
        RAISE EXCEPTION 'execution cycle, campaign and asset do not agree';
    END IF;
    IF v_config_asset IS DISTINCT FROM NEW.asset_id THEN
        RAISE EXCEPTION 'execution configuration does not belong to asset';
    END IF;
    IF v_campaign_kind IS DISTINCT FROM v_asset_kind
       OR v_campaign_kind IS DISTINCT FROM v_case_kind THEN
        RAISE EXCEPTION 'execution campaign, asset and test case kinds do not agree';
    END IF;

    IF v_asset_kind = 'WHOLE_MACHINE' THEN
        IF v_case_target IS DISTINCT FROM 'SYS' THEN
            RAISE EXCEPTION 'whole-machine execution requires SYS target, got %',
                v_case_target;
        END IF;
    ELSIF v_asset_kind = 'MODULE' THEN
        SELECT target_part_code INTO v_asset_target
        FROM test.module_profile
        WHERE asset_id = NEW.asset_id;

        IF v_asset_target IS NULL THEN
            RAISE EXCEPTION 'module asset % has no target part', NEW.asset_id;
        END IF;
        IF NOT (
            v_case_target = v_asset_target
            OR (v_case_target = 'ARM' AND v_asset_target = 'SARM')
            OR (v_case_target = 'LEG' AND v_asset_target = 'SLEG')
        ) THEN
            RAISE EXCEPTION 'test case target % does not match module asset target %',
                v_case_target, v_asset_target;
        END IF;
    END IF;
    RETURN NEW;
END;
$$;
