-- The MTBF formula is asset-count agnostic. Publish a separate MODULE scope
-- so module Campaigns remain isolated from whole-machine Campaigns while using
-- the same deterministic exposure/failure calculation contract.
INSERT INTO reliability.mtbf_scope (
    scope_code, name, asset_kind, version, description, status, published_at
) VALUES (
    'OVERALL', '模块整体 MTBF', 'MODULE', '1.0',
    '模块样品独立Campaign的总体MTBF范围；不与整机样品混算。',
    'PUBLISHED', clock_timestamp()
) ON CONFLICT (scope_code, asset_kind, version) DO UPDATE
SET name = EXCLUDED.name,
    description = EXCLUDED.description,
    status = 'PUBLISHED',
    published_at = coalesce(reliability.mtbf_scope.published_at, EXCLUDED.published_at),
    updated_at = clock_timestamp();
