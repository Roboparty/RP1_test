-- Resolve public API identifiers before querying RLS-protected rows so the
-- HTTP layer can distinguish a missing resource (404) from denied access
-- (403), as required by the MTBF backend contract.

CREATE OR REPLACE FUNCTION iam.campaign_access_state(
    p_campaign_public_id uuid,
    p_require_edit boolean DEFAULT false
)
RETURNS text
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, iam, test
AS $$
    SELECT CASE
        WHEN NOT EXISTS (
            SELECT 1 FROM test.test_campaign c
            WHERE c.public_id = p_campaign_public_id
        ) THEN 'NOT_FOUND'
        WHEN EXISTS (
            SELECT 1 FROM test.test_campaign c
            WHERE c.public_id = p_campaign_public_id
              AND iam.can_access_campaign(c.id, p_require_edit)
        ) THEN 'ALLOWED'
        ELSE 'FORBIDDEN'
    END
$$;

CREATE OR REPLACE FUNCTION iam.interruption_access_state(
    p_interruption_public_id uuid,
    p_require_edit boolean DEFAULT false
)
RETURNS text
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, iam, reliability
AS $$
    SELECT CASE
        WHEN NOT EXISTS (
            SELECT 1 FROM reliability.interruption i
            WHERE i.public_id = p_interruption_public_id
        ) THEN 'NOT_FOUND'
        WHEN EXISTS (
            SELECT 1 FROM reliability.interruption i
            WHERE i.public_id = p_interruption_public_id
              AND iam.can_access_campaign(i.campaign_id, p_require_edit)
        ) THEN 'ALLOWED'
        ELSE 'FORBIDDEN'
    END
$$;

CREATE OR REPLACE FUNCTION iam.calculation_access_state(
    p_calculation_public_id uuid,
    p_require_edit boolean DEFAULT false
)
RETURNS text
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, iam, reliability
AS $$
    SELECT CASE
        WHEN NOT EXISTS (
            SELECT 1 FROM reliability.calculation_run r
            WHERE r.public_id = p_calculation_public_id
        ) THEN 'NOT_FOUND'
        WHEN EXISTS (
            SELECT 1 FROM reliability.calculation_run r
            WHERE r.public_id = p_calculation_public_id
              AND iam.can_access_calculation(r.id, p_require_edit)
        ) THEN 'ALLOWED'
        ELSE 'FORBIDDEN'
    END
$$;

REVOKE ALL ON FUNCTION iam.campaign_access_state(uuid, boolean) FROM PUBLIC;
REVOKE ALL ON FUNCTION iam.interruption_access_state(uuid, boolean) FROM PUBLIC;
REVOKE ALL ON FUNCTION iam.calculation_access_state(uuid, boolean) FROM PUBLIC;

GRANT EXECUTE ON FUNCTION iam.campaign_access_state(uuid, boolean)
TO __RP1_APP_USER__, __RP1_READONLY_USER__;
GRANT EXECUTE ON FUNCTION iam.interruption_access_state(uuid, boolean)
TO __RP1_APP_USER__, __RP1_READONLY_USER__;
GRANT EXECUTE ON FUNCTION iam.calculation_access_state(uuid, boolean)
TO __RP1_APP_USER__, __RP1_READONLY_USER__;
