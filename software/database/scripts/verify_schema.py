"""Read-only checks for database structure and export queue permissions."""
import os
import psycopg


def main() -> None:
    dsn = os.environ['DATABASE_URL'].replace('postgresql+psycopg://', 'postgresql://', 1)
    with psycopg.connect(dsn) as connection, connection.cursor() as cursor:
        cursor.execute('SELECT version_num FROM alembic_version')
        assert cursor.fetchone()[0] == '20261006_0026', 'migrations are not at the expected head'
        for relation in [
            'catalog.test_case_version', 'test.test_execution', 'test.execution_result',
            'health.metric_observation', 'integration.ingestion_receipt',
            'integration.influx_export_queue',
        ]:
            cursor.execute('SELECT to_regclass(%s)', (relation,))
            assert cursor.fetchone()[0] is not None, f'missing {relation}'
        cursor.execute("""
            SELECT count(*) FROM catalog.telemetry_subject
            WHERE enabled AND target_part_code = 'SYS'
              AND subject_code = ANY(%s)
        """, ([
            'SYS-SHOULDER-PITCH-R', 'SYS-SHOULDER-ROLL-R', 'SYS-SHOULDER-YAW-R',
            'SYS-ELBOW-PITCH-R', 'SYS-WRIST-ROLL-R', 'SYS-WRIST-YAW-R', 'SYS-WRIST-PITCH-R',
        ],))
        assert cursor.fetchone()[0] == 7, 'whole-machine right-arm directory is incomplete'
        cursor.execute("SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE oid = 'integration.influx_export_queue'::regclass")
        assert cursor.fetchone() == (True, True), 'export queue must enforce RLS'
    print('Schema version, fact tables, telemetry subjects and queue permissions are ready.')


if __name__ == '__main__':
    main()
