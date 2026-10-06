-- Migration 008 was already applied in the local Docker database before the
-- service-principal campaign access correction was identified.
CREATE OR REPLACE FUNCTION iam.can_access_campaign(
    p_campaign_id bigint,
    p_require_edit boolean DEFAULT false
)
RETURNS boolean
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, iam
AS $$
    SELECT CASE
        WHEN p_campaign_id IS NULL THEN false
        WHEN iam.is_internal_service() THEN true
        WHEN iam.is_system_admin() THEN true
        WHEN p_require_edit AND iam.current_user_role() <> 'TEST_EXECUTOR' THEN false
        ELSE EXISTS (
            SELECT 1
            FROM iam.user_campaign_access uca
            WHERE uca.user_id = iam.current_user_id()
              AND uca.campaign_id = p_campaign_id
              AND (NOT p_require_edit OR uca.access_level = 'EDIT')
        )
    END
$$;

CREATE INDEX IF NOT EXISTS idx_campaign_mtbf_config_enabled
    ON reliability.campaign_mtbf_config(enabled, campaign_id, scope_id)
    WHERE enabled;

CREATE INDEX IF NOT EXISTS idx_campaign_mtbf_config_scope
    ON reliability.campaign_mtbf_config(scope_id, campaign_id);
