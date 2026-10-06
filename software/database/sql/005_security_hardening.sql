-- Keep authentication material inaccessible to both shared application roles.
-- This statement intentionally follows all broad schema-level grants.
REVOKE ALL ON TABLE iam.user_credential, iam.auth_session
FROM __RP1_APP_USER__, __RP1_READONLY_USER__;

-- PostgreSQL grants function execution to PUBLIC by default.  Restrict the two
-- SECURITY DEFINER entry points that either establish identity or mutate work.
REVOKE ALL ON FUNCTION iam.set_request_context(uuid, text, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION iam.set_request_context(uuid, text, text)
TO __RP1_APP_USER__, __RP1_READONLY_USER__;

REVOKE ALL ON FUNCTION reliability.claim_recompute_job(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION reliability.claim_recompute_job(text)
TO __RP1_APP_USER__;
