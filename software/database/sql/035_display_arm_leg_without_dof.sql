-- Operator pages show 单臂 and 单腿. The sample code still records the degree variant.

UPDATE catalog.test_target_part
SET name = '单臂',
    description = '单臂模块',
    updated_at = clock_timestamp()
WHERE part_code IN ('ARM', 'SARM');

UPDATE catalog.test_target_part
SET name = '单腿',
    description = '单腿模块',
    updated_at = clock_timestamp()
WHERE part_code IN ('LEG', 'SLEG');
