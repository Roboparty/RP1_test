INSERT INTO integration.ingestion_source (
    source_code, source_type, direction, status, config_reference
) VALUES
    ('SYSTEM', 'SYSTEM', 'INTERNAL', 'ACTIVE', 'built-in'),
    ('MANUAL', 'MANUAL', 'INBOUND', 'ACTIVE', 'operator API')
ON CONFLICT (source_code) DO NOTHING;

INSERT INTO catalog.mission_tag (tag_code, name, description) VALUES
    ('GENERAL_OPERATION', '通用运行', '没有更具体任务标签的普通运行'),
    ('LOAD_WALK', '负载行走', '携带负载执行行走任务'),
    ('LOAD_STAND', '负载站立', '携带负载保持站立'),
    ('GRASP', '抓取', '抓取或搬运任务'),
    ('FACTORY_WORK', '工厂负载作业', '工厂作业任务族标签'),
    ('DEMO_INTERACTION', '展示交互', '展示或人机交互任务'),
    ('HIGH_DIFFICULTY', '高难场景', '非普通作业的高难测试场景'),
    ('FAULT_INJECTION', '故障注入', '不直接作为常规正式 MTBF 暴露'),
    ('ENVIRONMENTAL_EVIDENCE', '环境专项证据', '环境、EMC、振动等独立证据')
ON CONFLICT (tag_code) DO NOTHING;

INSERT INTO reliability.mtbf_scope (
    scope_code, name, asset_kind, version, description, status
) VALUES
    ('OVERALL', '整机整体 MTBF', 'WHOLE_MACHINE', '1.0-draft', '规则明细后续配置', 'DRAFT'),
    ('OVERALL', '模块整体 MTBF', 'MODULE', '1.0-draft', '规则明细后续配置', 'DRAFT'),
    ('FACTORY_LOAD', '工厂负载作业 MTBF', 'WHOLE_MACHINE', '1.0-draft', '规则明细后续配置', 'DRAFT'),
    ('DEMO_INTERACTION', '展示交互 MTBF', 'WHOLE_MACHINE', '1.0-draft', '规则明细后续配置', 'DRAFT')
ON CONFLICT (scope_code, asset_kind, version) DO NOTHING;

INSERT INTO reliability.statistics_method (
    method_code, version, name, parameter_schema,
    default_parameters, implementation_key, status
) VALUES (
    'UNCONFIGURED',
    '1.0-draft',
    '待选定正式 MTBF 统计方法',
    '{"type":"object","additionalProperties":true}'::jsonb,
    '{}'::jsonb,
    'mtbf.method.unconfigured',
    'DRAFT'
)
ON CONFLICT (method_code, version) DO NOTHING;

INSERT INTO integration.retention_policy (
    policy_code, version, data_class, retention_spec, status
) VALUES
    ('TELEMETRY_RAW', '1.0-draft', 'INFLUX_RAW', '{"configured":false,"protectEvidence":true}', 'DRAFT'),
    ('TELEMETRY_ROLLUP', '1.0-draft', 'INFLUX_ROLLUP', '{"configured":false,"protectEvidence":true}', 'DRAFT'),
    ('ARTIFACT', '1.0-draft', 'NAS_ARTIFACT', '{"configured":false,"protectEvidence":true}', 'DRAFT')
ON CONFLICT (policy_code, version) DO NOTHING;
