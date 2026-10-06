ALTER TABLE test.test_execution
    ADD COLUMN code_timestamp timestamptz;

UPDATE test.test_execution
SET code_timestamp = COALESCE(normalized_started_at, source_time, created_at);

DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM test.test_execution
        WHERE station_id IS NULL
    ) THEN
        RAISE EXCEPTION
            'Cannot enforce execution identifier rule: an execution has no station/location';
    END IF;
END
$$;

ALTER TABLE test.test_execution
    ALTER COLUMN code_timestamp SET NOT NULL,
    ALTER COLUMN station_id SET NOT NULL;

CREATE OR REPLACE FUNCTION test.compose_execution_code(
    p_test_case_code text,
    p_code_timestamp timestamptz,
    p_asset_code text,
    p_station_code text
)
RETURNS text
LANGUAGE sql
STABLE
STRICT
AS $$
    SELECT
        upper(p_test_case_code)
        || '_'
        || to_char(p_code_timestamp AT TIME ZONE 'Asia/Shanghai', 'YYMMDDHH24MI')
        || '_'
        || upper(p_asset_code)
        || '_'
        || upper(p_station_code)
$$;

DO $$
BEGIN
    IF EXISTS (
        SELECT expected_code
        FROM (
            SELECT test.compose_execution_code(
                tc.case_code,
                e.code_timestamp,
                a.asset_code,
                s.station_code
            ) AS expected_code
            FROM test.test_execution e
            JOIN catalog.test_case_version tcv ON tcv.id = e.test_case_version_id
            JOIN catalog.test_case tc ON tc.id = tcv.test_case_id
            JOIN test.asset a ON a.id = e.asset_id
            JOIN test.station s ON s.id = e.station_id
        ) codes
        GROUP BY expected_code
        HAVING count(*) > 1
    ) THEN
        RAISE EXCEPTION
            'Cannot enforce execution identifier rule: duplicate case/time/asset/station combination';
    END IF;
END
$$;

UPDATE test.test_execution e
SET execution_code = test.compose_execution_code(
    tc.case_code,
    e.code_timestamp,
    a.asset_code,
    s.station_code
)
FROM catalog.test_case_version tcv
JOIN catalog.test_case tc ON tc.id = tcv.test_case_id
JOIN test.asset a ON true
JOIN test.station s ON true
WHERE tcv.id = e.test_case_version_id
  AND a.id = e.asset_id
  AND s.id = e.station_id;

ALTER TABLE test.test_execution
    ADD CONSTRAINT ck_execution_code_case_time_asset_station
    CHECK (
        execution_code ~
        '^[A-Z0-9]+(-[A-Z0-9]+)*_[0-9]{10}_RP[0-9]+\.[0-9]+-(SARM|SLEG|SYS|UPPER|LOWER|CHEST|HEAD|BAT)-[0-9]{3}_[A-Z0-9]+(-[A-Z0-9]+)*$'
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

    NEW.execution_code := test.compose_execution_code(
        v_test_case_code,
        NEW.code_timestamp,
        v_asset_code,
        v_station_code
    );
    RETURN NEW;
END
$$;

REVOKE ALL ON FUNCTION test.enforce_execution_code() FROM PUBLIC;

DROP TRIGGER IF EXISTS trg_enforce_execution_code ON test.test_execution;
CREATE TRIGGER trg_enforce_execution_code
BEFORE INSERT OR UPDATE OF
    execution_code,
    test_case_version_id,
    asset_id,
    station_id,
    code_timestamp
ON test.test_execution
FOR EACH ROW
EXECUTE FUNCTION test.enforce_execution_code();

COMMENT ON COLUMN test.test_execution.code_timestamp IS
    'Identifier timestamp rendered as YYMMDDHHMM in Asia/Shanghai.';
COMMENT ON COLUMN test.test_execution.execution_code IS
    'Database-generated identifier: test case_YYMMDDHHMM_asset_station/location.';
