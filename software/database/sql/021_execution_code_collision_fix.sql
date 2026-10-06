CREATE OR REPLACE FUNCTION test.compose_execution_code_v2(
    p_test_case_code text,
    p_code_timestamp timestamptz,
    p_asset_code text,
    p_station_code text,
    p_execution_id bigint
)
RETURNS text
LANGUAGE sql
STABLE
STRICT
AS $$
    SELECT test.compose_execution_code(
        p_test_case_code,
        p_code_timestamp,
        p_asset_code,
        p_station_code
    ) || '_E' || p_execution_id::text
$$;

REVOKE ALL ON FUNCTION test.compose_execution_code_v2(
    text, timestamptz, text, text, bigint
) FROM PUBLIC;

ALTER TABLE test.test_execution
    DROP CONSTRAINT ck_execution_code_case_time_asset_station;

ALTER TABLE test.test_execution
    ADD CONSTRAINT ck_execution_code_case_time_asset_station
    CHECK (
        execution_code ~
        '^[A-Z0-9]+(-[A-Z0-9]+)*_[0-9]{10}_RP[0-9]+\.[0-9]+-(SARM|SLEG|SYS|UPPER|LOWER|CHEST|HEAD|BAT)-[0-9]{3}_[A-Z0-9]+(-[A-Z0-9]+)*(_E[0-9]+)?$'
    ) NOT VALID;

ALTER TABLE test.test_execution
    VALIDATE CONSTRAINT ck_execution_code_case_time_asset_station;

CREATE OR REPLACE FUNCTION test.enforce_execution_code()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE
    v_test_case_code text;
    v_asset_code text;
    v_station_code text;
BEGIN
    IF NEW.code_timestamp IS NULL THEN
        NEW.code_timestamp := COALESCE(
            NEW.normalized_started_at,
            NEW.source_time,
            NEW.created_at,
            clock_timestamp()
        );
    END IF;

    IF NEW.id IS NULL THEN
        RAISE EXCEPTION 'Execution identity must be assigned before composing its code';
    END IF;

    SELECT tc.case_code
    INTO STRICT v_test_case_code
    FROM catalog.test_case_version tcv
    JOIN catalog.test_case tc ON tc.id = tcv.test_case_id
    WHERE tcv.id = NEW.test_case_version_id;

    SELECT a.asset_code
    INTO STRICT v_asset_code
    FROM test.asset a
    WHERE a.id = NEW.asset_id;

    SELECT s.station_code
    INTO STRICT v_station_code
    FROM test.station s
    WHERE s.id = NEW.station_id;

    NEW.execution_code := test.compose_execution_code_v2(
        v_test_case_code,
        NEW.code_timestamp,
        v_asset_code,
        v_station_code,
        NEW.id
    );
    RETURN NEW;
END
$$;

REVOKE ALL ON FUNCTION test.enforce_execution_code() FROM PUBLIC;

COMMENT ON COLUMN test.test_execution.execution_code IS
    'Database-generated identifier: test case_YYMMDDHHMM_asset_station_E<execution identity>; legacy rows omit the identity suffix.';
