ALTER TABLE catalog.test_case
    DROP CONSTRAINT IF EXISTS fk_test_case_target_part_kind;
ALTER TABLE test.module_profile
    DROP CONSTRAINT IF EXISTS module_profile_target_part_code_fkey;

UPDATE catalog.test_target_part
SET part_code = CASE part_code
    WHEN 'WHOLE_MACHINE' THEN 'SYS'
    WHEN 'SINGLE_ARM' THEN 'SARM'
    WHEN 'SINGLE_LEG' THEN 'SLEG'
    WHEN 'UPPER_BODY' THEN 'UPPER'
    WHEN 'LOWER_BODY' THEN 'LOWER'
    WHEN 'BATTERY' THEN 'BAT'
    ELSE part_code
END,
updated_at = clock_timestamp();

UPDATE catalog.test_case
SET target_part_code = CASE target_part_code
    WHEN 'WHOLE_MACHINE' THEN 'SYS'
    WHEN 'SINGLE_ARM' THEN 'SARM'
    WHEN 'SINGLE_LEG' THEN 'SLEG'
    WHEN 'UPPER_BODY' THEN 'UPPER'
    WHEN 'LOWER_BODY' THEN 'LOWER'
    WHEN 'BATTERY' THEN 'BAT'
    ELSE target_part_code
END,
updated_at = clock_timestamp();

UPDATE test.module_profile
SET target_part_code = CASE target_part_code
    WHEN 'SINGLE_ARM' THEN 'SARM'
    WHEN 'SINGLE_LEG' THEN 'SLEG'
    WHEN 'UPPER_BODY' THEN 'UPPER'
    WHEN 'LOWER_BODY' THEN 'LOWER'
    WHEN 'BATTERY' THEN 'BAT'
    ELSE target_part_code
END,
updated_at = clock_timestamp();

INSERT INTO catalog.test_target_part(
    part_code, name, asset_kind, description, enabled
) VALUES (
    'HEAD', '头部', 'MODULE', '头部模块', true
)
ON CONFLICT (part_code) DO UPDATE SET
    name = EXCLUDED.name,
    asset_kind = EXCLUDED.asset_kind,
    description = EXCLUDED.description,
    enabled = true,
    updated_at = clock_timestamp();

ALTER TABLE catalog.test_case
    ADD CONSTRAINT fk_test_case_target_part_kind
    FOREIGN KEY (target_part_code, asset_kind)
    REFERENCES catalog.test_target_part(part_code, asset_kind);
ALTER TABLE test.module_profile
    ADD CONSTRAINT module_profile_target_part_code_fkey
    FOREIGN KEY (target_part_code)
    REFERENCES catalog.test_target_part(part_code);

ALTER TABLE catalog.test_target_part
    ADD COLUMN asset_code_segment text;

UPDATE catalog.test_target_part
SET asset_code_segment = part_code,
    updated_at = clock_timestamp();

ALTER TABLE catalog.test_target_part
    ALTER COLUMN asset_code_segment SET NOT NULL,
    ADD CONSTRAINT uq_test_target_part_asset_code_segment
        UNIQUE (asset_code_segment),
    ADD CONSTRAINT ck_test_target_part_asset_code_segment
        CHECK (
            part_code IN ('SARM', 'SLEG', 'SYS', 'UPPER', 'LOWER', 'CHEST', 'HEAD', 'BAT')
            AND asset_code_segment = part_code
        );

UPDATE test.asset
SET asset_code = CASE asset_code
    WHEN 'SHOWCASE-RP1-023' THEN 'RP1.3-SYS-023'
    WHEN 'SHOWCASE-RP1-031' THEN 'RP1.3-SYS-031'
    WHEN 'SHOWCASE-RP1-038' THEN 'RP1.3-SYS-038'
    WHEN 'SHOWCASE-RP1-044' THEN 'RP1.3-SYS-044'
    WHEN 'SHOWCASE-JNT-KNEE-014' THEN 'RP1.3-SLEG-014'
    WHEN 'SHOWCASE-JNT-HIP-009' THEN 'RP1.3-SLEG-009'
    WHEN 'SHOWCASE-HAND-L-022' THEN 'RP1.3-SARM-022'
    WHEN 'SHOWCASE-BAT-018' THEN 'RP1.3-BAT-018'
END,
updated_at = clock_timestamp()
WHERE asset_code IN (
    'SHOWCASE-RP1-023',
    'SHOWCASE-RP1-031',
    'SHOWCASE-RP1-038',
    'SHOWCASE-RP1-044',
    'SHOWCASE-JNT-KNEE-014',
    'SHOWCASE-JNT-HIP-009',
    'SHOWCASE-HAND-L-022',
    'SHOWCASE-BAT-018'
);

ALTER TABLE test.asset
    ADD CONSTRAINT ck_asset_code_version_part_sequence
    CHECK (asset_code ~ '^RP[0-9]+\.[0-9]+-[A-Z][A-Z0-9]*-[0-9]{3}$')
    NOT VALID;

ALTER TABLE test.asset
    VALIDATE CONSTRAINT ck_asset_code_version_part_sequence;

CREATE OR REPLACE FUNCTION test.validate_module_profile_target()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    v_asset_kind text;
    v_asset_code text;
    v_asset_segment text;
    v_part_kind text;
    v_part_segment text;
BEGIN
    SELECT asset_kind, asset_code,
           split_part(asset_code, '-', 2)
    INTO v_asset_kind, v_asset_code, v_asset_segment
    FROM test.asset
    WHERE id = NEW.asset_id;

    IF v_asset_kind IS DISTINCT FROM 'MODULE' THEN
        RAISE EXCEPTION 'module profile asset % is not MODULE', NEW.asset_id;
    END IF;

    IF NEW.target_part_code IS NOT NULL THEN
        SELECT asset_kind, asset_code_segment
        INTO v_part_kind, v_part_segment
        FROM catalog.test_target_part
        WHERE part_code = NEW.target_part_code AND enabled;

        IF v_part_kind IS DISTINCT FROM 'MODULE' THEN
            RAISE EXCEPTION 'module profile target part % is not an enabled MODULE part',
                NEW.target_part_code;
        END IF;
        IF v_asset_segment IS DISTINCT FROM v_part_segment THEN
            RAISE EXCEPTION
                'asset code % segment % does not match target part % segment %',
                v_asset_code, v_asset_segment, NEW.target_part_code, v_part_segment;
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION test.validate_asset_code_target()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    v_asset_segment text;
    v_profile_target text;
    v_expected_segment text;
BEGIN
    v_asset_segment := split_part(NEW.asset_code, '-', 2);

    IF NEW.asset_kind = 'WHOLE_MACHINE' THEN
        IF v_asset_segment IS DISTINCT FROM 'SYS' THEN
            RAISE EXCEPTION 'whole-machine asset code % must use SYS segment',
                NEW.asset_code;
        END IF;
    ELSIF NEW.asset_kind = 'MODULE' THEN
        SELECT mp.target_part_code, part.asset_code_segment
        INTO v_profile_target, v_expected_segment
        FROM test.module_profile mp
        LEFT JOIN catalog.test_target_part part
          ON part.part_code = mp.target_part_code
        WHERE mp.asset_id = NEW.id;

        IF FOUND AND v_asset_segment IS DISTINCT FROM v_expected_segment THEN
            RAISE EXCEPTION
                'asset code % segment % does not match profile target % segment %',
                NEW.asset_code, v_asset_segment, v_profile_target, v_expected_segment;
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

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
        IF v_case_target IS DISTINCT FROM v_asset_target THEN
            RAISE EXCEPTION 'test case target % does not match module asset target %',
                v_case_target, v_asset_target;
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_asset_code_target
BEFORE INSERT OR UPDATE OF asset_code, asset_kind ON test.asset
FOR EACH ROW EXECUTE FUNCTION test.validate_asset_code_target();
