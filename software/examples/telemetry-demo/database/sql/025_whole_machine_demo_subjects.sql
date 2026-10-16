-- Explicit whole-machine joint coordinates for the synthetic right-arm example.
-- Preserve module/SYS separation and existing whole-machine seed display orders.
INSERT INTO catalog.telemetry_subject(subject_code, name, subject_kind, target_part_code, display_order)
VALUES
    ('SYS-SHOULDER-PITCH-R', '整机右肩俯仰关节', 'JOINT', 'SYS', 3),
    ('SYS-SHOULDER-ROLL-R', '整机右肩滚转关节', 'JOINT', 'SYS', 4),
    ('SYS-SHOULDER-YAW-R', '整机右肩偏航关节', 'JOINT', 'SYS', 5),
    ('SYS-ELBOW-PITCH-R', '整机右肘俯仰关节', 'JOINT', 'SYS', 6),
    ('SYS-WRIST-ROLL-R', '整机右腕滚转关节', 'JOINT', 'SYS', 7),
    ('SYS-WRIST-YAW-R', '整机右腕偏航关节', 'JOINT', 'SYS', 8),
    ('SYS-WRIST-PITCH-R', '整机右腕俯仰关节', 'JOINT', 'SYS', 9)
ON CONFLICT(subject_code) DO UPDATE SET
    name = EXCLUDED.name, subject_kind = EXCLUDED.subject_kind,
    target_part_code = EXCLUDED.target_part_code, enabled = true;
