-- Every enabled human VIEWER/TEST_EXECUTOR can read every campaign by default.
-- EDIT remains an explicit grant through the existing approval workflow.

CREATE OR REPLACE FUNCTION iam.default_grantor_id()
RETURNS bigint
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, iam
AS $$
    SELECT coalesce(
        iam.current_user_id(),
        (SELECT id FROM iam.app_user
         WHERE role = 'SYSTEM_ADMIN' AND principal_kind = 'HUMAN' AND enabled
         ORDER BY id LIMIT 1)
    )
$$;

CREATE OR REPLACE FUNCTION iam.grant_default_view_for_campaign()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, iam, test
AS $$
DECLARE
    v_grantor bigint := iam.default_grantor_id();
BEGIN
    IF v_grantor IS NULL THEN
        RETURN NEW;
    END IF;
    INSERT INTO iam.user_campaign_access(user_id, campaign_id, access_level, granted_by, granted_at)
    SELECT u.id, NEW.id, 'VIEW', v_grantor, clock_timestamp()
    FROM iam.app_user u
    WHERE u.principal_kind = 'HUMAN'
      AND u.enabled
      AND u.role IN ('VIEWER', 'TEST_EXECUTOR')
    ON CONFLICT (user_id, campaign_id) DO NOTHING;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION iam.grant_default_view_for_user()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, iam, test
AS $$
DECLARE
    v_grantor bigint := iam.default_grantor_id();
BEGIN
    IF v_grantor IS NULL
       OR NEW.principal_kind <> 'HUMAN'
       OR NOT NEW.enabled
       OR NEW.role NOT IN ('VIEWER', 'TEST_EXECUTOR') THEN
        RETURN NEW;
    END IF;
    INSERT INTO iam.user_campaign_access(user_id, campaign_id, access_level, granted_by, granted_at)
    SELECT NEW.id, c.id, 'VIEW', v_grantor, clock_timestamp()
    FROM test.test_campaign c
    ON CONFLICT (user_id, campaign_id) DO NOTHING;
    RETURN NEW;
END;
$$;

REVOKE ALL ON FUNCTION iam.default_grantor_id() FROM PUBLIC;
REVOKE ALL ON FUNCTION iam.grant_default_view_for_campaign() FROM PUBLIC;
REVOKE ALL ON FUNCTION iam.grant_default_view_for_user() FROM PUBLIC;

DROP TRIGGER IF EXISTS trg_default_campaign_view ON test.test_campaign;
CREATE TRIGGER trg_default_campaign_view
    AFTER INSERT ON test.test_campaign
    FOR EACH ROW EXECUTE FUNCTION iam.grant_default_view_for_campaign();

DROP TRIGGER IF EXISTS trg_default_campaign_view ON iam.app_user;
CREATE TRIGGER trg_default_campaign_view
    AFTER INSERT ON iam.app_user
    FOR EACH ROW EXECUTE FUNCTION iam.grant_default_view_for_user();

INSERT INTO iam.user_campaign_access(user_id, campaign_id, access_level, granted_by, granted_at)
SELECT u.id, c.id, 'VIEW', iam.default_grantor_id(), clock_timestamp()
FROM iam.app_user u
CROSS JOIN test.test_campaign c
WHERE u.principal_kind = 'HUMAN'
  AND u.enabled
  AND u.role IN ('VIEWER', 'TEST_EXECUTOR')
  AND iam.default_grantor_id() IS NOT NULL
ON CONFLICT (user_id, campaign_id) DO NOTHING;
