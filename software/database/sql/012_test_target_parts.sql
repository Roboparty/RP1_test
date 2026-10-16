CREATE TABLE catalog.test_target_part (
    part_code text PRIMARY KEY,
    name text NOT NULL,
    asset_kind text NOT NULL CHECK (asset_kind IN ('WHOLE_MACHINE', 'MODULE')),
    description text NOT NULL DEFAULT '',
    enabled boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (part_code, asset_kind)
);

INSERT INTO catalog.test_target_part(part_code, name, asset_kind, description)
VALUES
    ('SYS', '整机', 'WHOLE_MACHINE', '完整整机测试对象'),
    ('SARM', '单臂', 'MODULE', '单臂模块'),
    ('SLEG', '单腿', 'MODULE', '单腿模块'),
    ('CHEST', '胸腔', 'MODULE', '胸腔模块'),
    ('UPPER', '上肢', 'MODULE', '腰部与双臂模块'),
    ('LOWER', '下肢', 'MODULE', '双腿与腰部模块'),
    ('HEAD', '头部', 'MODULE', '头部模块'),
    ('BAT', '动力电池', 'MODULE', '动力电池模块')
ON CONFLICT (part_code) DO UPDATE SET
    name = EXCLUDED.name,
    asset_kind = EXCLUDED.asset_kind,
    description = EXCLUDED.description,
    enabled = true,
    updated_at = clock_timestamp();

ALTER TABLE catalog.test_case
    ADD COLUMN target_part_code text;

ALTER TABLE catalog.test_case
    ADD CONSTRAINT fk_test_case_target_part_kind
    FOREIGN KEY (target_part_code, asset_kind)
    REFERENCES catalog.test_target_part(part_code, asset_kind);

ALTER TABLE catalog.test_case
    ADD CONSTRAINT ck_test_case_enabled_target_part
    CHECK (NOT enabled OR target_part_code IS NOT NULL) NOT VALID;

CREATE INDEX ix_test_case_enabled_target_part
    ON catalog.test_case(target_part_code, case_code)
    WHERE enabled;

UPDATE catalog.test_case
SET enabled = false,
    target_part_code = NULL,
    updated_at = clock_timestamp()
WHERE case_code LIKE 'SHOWCASE-%-TC-%';

ALTER TABLE test.module_profile
    ADD COLUMN target_part_code text
    REFERENCES catalog.test_target_part(part_code);

CREATE INDEX ix_module_profile_target_part
    ON test.module_profile(target_part_code);

CREATE OR REPLACE FUNCTION test.validate_module_profile_target()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    v_asset_kind text;
    v_part_kind text;
BEGIN
    SELECT asset_kind INTO v_asset_kind
    FROM test.asset
    WHERE id = NEW.asset_id;

    IF v_asset_kind IS DISTINCT FROM 'MODULE' THEN
        RAISE EXCEPTION 'module profile asset % is not MODULE', NEW.asset_id;
    END IF;

    IF NEW.target_part_code IS NOT NULL THEN
        SELECT asset_kind INTO v_part_kind
        FROM catalog.test_target_part
        WHERE part_code = NEW.target_part_code AND enabled;

        IF v_part_kind IS DISTINCT FROM 'MODULE' THEN
            RAISE EXCEPTION 'module profile target part % is not an enabled MODULE part',
                NEW.target_part_code;
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_module_profile_target
BEFORE INSERT OR UPDATE OF asset_id, target_part_code ON test.module_profile
FOR EACH ROW EXECUTE FUNCTION test.validate_module_profile_target();

UPDATE test.module_profile mp
SET target_part_code = mapped.target_part_code,
    updated_at = clock_timestamp()
FROM test.asset a
JOIN (
    VALUES
        ('SHOWCASE-JNT-KNEE-014', 'SLEG'),
        ('SHOWCASE-JNT-HIP-009', 'SLEG'),
        ('SHOWCASE-HAND-L-022', 'SARM'),
        ('SHOWCASE-BAT-018', 'BAT')
) AS mapped(asset_code, target_part_code)
  ON mapped.asset_code = a.asset_code
WHERE mp.asset_id = a.id;

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

CREATE TRIGGER trg_set_updated_at
BEFORE UPDATE ON catalog.test_target_part
FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();

CREATE TRIGGER trg_audit_change
AFTER INSERT OR UPDATE OR DELETE ON catalog.test_target_part
FOR EACH ROW EXECUTE FUNCTION audit.capture_row_change();

ALTER TABLE catalog.test_target_part ENABLE ROW LEVEL SECURITY;
CREATE POLICY authenticated_select ON catalog.test_target_part
    FOR SELECT USING (iam.is_authenticated());
CREATE POLICY admin_write ON catalog.test_target_part
    FOR ALL USING (iam.is_system_admin()) WITH CHECK (iam.is_system_admin());

GRANT SELECT, INSERT, UPDATE ON catalog.test_target_part TO rp1_app;
GRANT SELECT ON catalog.test_target_part TO rp1_readonly;
