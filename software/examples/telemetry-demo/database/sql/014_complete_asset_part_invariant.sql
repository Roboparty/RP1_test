ALTER TABLE test.asset
    ADD CONSTRAINT ck_asset_code_allowed_part_segment
    CHECK (
        split_part(asset_code, '-', 2)
        IN ('SARM', 'SLEG', 'SYS', 'UPPER', 'LOWER', 'CHEST', 'HEAD', 'BAT')
    ) NOT VALID;

ALTER TABLE test.asset
    VALIDATE CONSTRAINT ck_asset_code_allowed_part_segment;

ALTER TABLE test.module_profile
    ALTER COLUMN target_part_code SET NOT NULL;
