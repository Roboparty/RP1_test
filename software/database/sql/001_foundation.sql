CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS btree_gist;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE OR REPLACE FUNCTION public.uuid_v7()
RETURNS uuid
LANGUAGE plpgsql
VOLATILE
PARALLEL SAFE
SET search_path = pg_catalog, public
AS $$
DECLARE
    unix_ts_ms bytea;
    random_bytes bytea;
BEGIN
    unix_ts_ms := substring(
        int8send((extract(epoch FROM clock_timestamp()) * 1000)::bigint)
        FROM 3
    );
    random_bytes := gen_random_bytes(10);
    random_bytes := set_byte(
        random_bytes,
        0,
        (get_byte(random_bytes, 0) & 15) | 112
    );
    random_bytes := set_byte(
        random_bytes,
        2,
        (get_byte(random_bytes, 2) & 63) | 128
    );
    RETURN encode(unix_ts_ms || random_bytes, 'hex')::uuid;
END;
$$;

COMMENT ON FUNCTION public.uuid_v7() IS
'Application-facing time-ordered UUIDv7 generator. Internal relations use bigint identity keys.';
