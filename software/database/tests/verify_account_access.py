from __future__ import annotations

import os
from contextlib import contextmanager
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.errors import (
    InsufficientPrivilege,
    UniqueViolation,
)


def dsn() -> str:
    return os.environ["DATABASE_URL"].replace(
        "postgresql+psycopg://", "postgresql://", 1
    )


@contextmanager
def savepoint(connection: psycopg.Connection, name: str):
    with connection.cursor() as cursor:
        cursor.execute(sql.SQL("SAVEPOINT {}").format(sql.Identifier(name)))
    try:
        yield
    except Exception:
        with connection.cursor() as cursor:
            cursor.execute(
                sql.SQL("ROLLBACK TO SAVEPOINT {}").format(sql.Identifier(name))
            )
        raise
    else:
        with connection.cursor() as cursor:
            cursor.execute(
                sql.SQL("RELEASE SAVEPOINT {}").format(sql.Identifier(name))
            )


def expect_error(connection, expected, statement: str, params=()) -> None:
    try:
        with savepoint(connection, f"expected_{uuid4().hex[:10]}"):
            with connection.cursor() as cursor:
                cursor.execute(statement, params)
    except expected:
        return
    raise AssertionError(f"expected {expected.__name__}: {statement}")


def scalar(cursor, statement: str, params=()):
    cursor.execute(statement, params)
    row = cursor.fetchone()
    return None if row is None else row[0]


def main() -> int:
    app_role = os.environ.get("RP1_APP_USER", "rp1_app")
    with psycopg.connect(dsn(), autocommit=False) as connection:
        with connection.cursor() as cursor:
            suffix = uuid4().hex[:10]
            bootstrap_hash = (
                "$argon2id$v=19$m=65536,t=3,p=4$NhwzPN6p+i8wvSq/f4V4vQ$"
                "2JszfwV08Ei03zxCvTuwUnEmLxNboDtgfF25UL1JKJA"
            )
            cursor.execute(
                """
                DELETE FROM iam.user_credential
                WHERE user_id IN (
                    SELECT id FROM iam.app_user WHERE principal_kind = 'HUMAN'
                )
                """
            )
            cursor.execute(
                sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(app_role))
            )
            assert scalar(
                cursor,
                """
                SELECT iam.bootstrap_initial_admin(
                    'local-admin', 'Bootstrap verification', %s,
                    'bootstrap-verification', '127.0.0.1'
                )
                """,
                (bootstrap_hash,),
            )
            assert not scalar(
                cursor,
                """
                SELECT iam.bootstrap_initial_admin(
                    'local-admin', 'Must not overwrite', %s,
                    'bootstrap-repeat', '127.0.0.1'
                )
                """,
                (bootstrap_hash,),
            )
            cursor.execute("RESET ROLE")
            cursor.execute(
                """
                SELECT u.role, u.enabled, u.must_change_password, c.password_hash
                FROM iam.app_user u
                JOIN iam.user_credential c ON c.user_id = u.id
                WHERE u.username = 'local-admin'
                """
            )
            assert cursor.fetchone() == ("SYSTEM_ADMIN", True, True, bootstrap_hash)
            assert scalar(
                cursor,
                """
                SELECT count(*) FROM audit.security_event
                WHERE event_type = 'INITIAL_ADMIN_BOOTSTRAP'
                  AND request_id = 'bootstrap-verification'
                """,
            ) == 1
            cursor.execute(
                """
                INSERT INTO iam.app_user(
                    username, display_name, role, principal_kind,
                    enabled, must_change_password
                ) VALUES
                    (%s, 'Security Admin', 'SYSTEM_ADMIN', 'HUMAN', true, false),
                    (%s, 'Security Viewer', 'VIEWER', 'HUMAN', true, false),
                    (%s, 'Security Executor', 'TEST_EXECUTOR', 'HUMAN', true, false),
                    (%s, 'Security Service', 'TEST_EXECUTOR', 'SERVICE', true, false)
                RETURNING id, public_id, username
                """,
                (
                    f"security-admin-{suffix}",
                    f"security-viewer-{suffix}",
                    f"security-executor-{suffix}",
                    f"security-service-{suffix}",
                ),
            )
            users = {
                row[2]: {"id": row[0], "public_id": row[1]}
                for row in cursor.fetchall()
            }
            admin = users[f"security-admin-{suffix}"]
            viewer = users[f"security-viewer-{suffix}"]
            executor = users[f"security-executor-{suffix}"]
            service = users[f"security-service-{suffix}"]
            for user in users.values():
                cursor.execute(
                    """
                    INSERT INTO iam.user_credential(user_id, password_hash)
                    VALUES (%s, '$argon2id$verification-placeholder')
                    """,
                    (user["id"],),
                )

            program_id = scalar(
                cursor,
                """
                INSERT INTO test.test_program(program_code, name, status, owner_id)
                VALUES (%s, 'Security verify', 'ACTIVE', %s)
                RETURNING id
                """,
                (f"SEC-P-{suffix}", admin["id"]),
            )
            cursor.execute(
                """
                INSERT INTO test.test_campaign(
                    program_id, campaign_code, name, asset_kind, status
                ) VALUES
                    (%s, %s, 'Security Campaign A', 'WHOLE_MACHINE', 'ACTIVE'),
                    (%s, %s, 'Security Campaign B', 'MODULE', 'ACTIVE')
                RETURNING id, public_id
                """,
                (
                    program_id,
                    f"SEC-C-A-{suffix}",
                    program_id,
                    f"SEC-C-B-{suffix}",
                ),
            )
            campaigns = cursor.fetchall()

            cursor.execute(
                sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(app_role))
            )
            expect_error(
                connection,
                InsufficientPrivilege,
                "SELECT * FROM iam.user_credential",
            )
            expect_error(
                connection,
                InsufficientPrivilege,
                "SELECT * FROM iam.auth_session",
            )

            lookup_count = scalar(
                cursor,
                "SELECT count(*) FROM iam.auth_lookup_login_credential(%s)",
                (f"security-viewer-{suffix}",),
            )
            assert lookup_count == 1
            missing_count = scalar(
                cursor,
                "SELECT count(*) FROM iam.auth_lookup_login_credential(%s)",
                (f"missing-{suffix}",),
            )
            assert missing_count == 0

            for attempt in range(5):
                cursor.execute(
                    "SELECT iam.auth_record_login_failure(%s, %s, '127.0.0.1')",
                    (f"security-viewer-{suffix}", f"failure-{attempt}"),
                )
            cursor.execute("RESET ROLE")
            cursor.execute(
                "SELECT failed_attempts, locked_until > now() "
                "FROM iam.user_credential WHERE user_id=%s",
                (viewer["id"],),
            )
            assert cursor.fetchone() == (5, True)
            cursor.execute(
                "UPDATE iam.user_credential SET failed_attempts=0, locked_until=NULL "
                "WHERE user_id=%s",
                (viewer["id"],),
            )

            cursor.execute(
                sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(app_role))
            )
            cursor.execute(
                "SELECT iam.set_request_context(%s, 'viewer-submit', '')",
                (viewer["public_id"],),
            )
            access_request_id = scalar(
                cursor,
                """
                SELECT iam.submit_campaign_access_request(
                    %s, 'VIEW', 'need reliability visibility',
                    'viewer-submit', '127.0.0.1'
                )
                """,
                (campaigns[0][1],),
            )
            expect_error(
                connection,
                UniqueViolation,
                """
                SELECT iam.submit_campaign_access_request(
                    %s, 'EDIT', 'duplicate pending',
                    'viewer-submit-duplicate', '127.0.0.1'
                )
                """,
                (campaigns[0][1],),
            )
            assert (
                scalar(cursor, "SELECT count(*) FROM iam.campaign_access_request")
                == 1
            )

            cursor.execute(
                "SELECT iam.set_request_context(%s, 'admin-approve', '')",
                (admin["public_id"],),
            )
            assert scalar(
                cursor,
                """
                SELECT iam.admin_decide_campaign_access_request(
                    %s, 'APPROVED', 'approved for verification',
                    'admin-approve', '127.0.0.1'
                )
                """,
                (access_request_id,),
            )
            cursor.execute("RESET ROLE")
            assert (
                scalar(
                    cursor,
                    "SELECT access_level FROM iam.user_campaign_access "
                    "WHERE user_id=%s AND campaign_id=%s",
                    (viewer["id"], campaigns[0][0]),
                )
                == "VIEW"
            )

            cursor.execute(
                """
                INSERT INTO iam.user_campaign_access(
                    user_id, campaign_id, access_level, granted_by
                ) VALUES (%s, %s, 'EDIT', %s)
                """,
                (executor["id"], campaigns[1][0], admin["id"]),
            )
            cursor.execute(
                """
                INSERT INTO iam.campaign_access_request(
                    requester_id, campaign_id, requested_level, reason
                ) VALUES (%s, %s, 'VIEW', 'verify no downgrade')
                RETURNING public_id
                """,
                (executor["id"], campaigns[1][0]),
            )
            downgrade_request_id = cursor.fetchone()[0]
            cursor.execute(
                sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(app_role))
            )
            cursor.execute(
                "SELECT iam.set_request_context(%s, 'admin-no-downgrade', '')",
                (admin["public_id"],),
            )
            cursor.execute(
                """
                SELECT iam.admin_decide_campaign_access_request(
                    %s, 'APPROVED', 'retain stronger grant',
                    'admin-no-downgrade', '127.0.0.1'
                )
                """,
                (downgrade_request_id,),
            )
            cursor.execute("RESET ROLE")
            assert (
                scalar(
                    cursor,
                    "SELECT access_level FROM iam.user_campaign_access "
                    "WHERE user_id=%s AND campaign_id=%s",
                    (executor["id"], campaigns[1][0]),
                )
                == "EDIT"
            )

            cursor.execute(
                """
                INSERT INTO iam.campaign_access_request(
                    requester_id, campaign_id, requested_level, reason
                ) VALUES (%s, %s, 'VIEW', 'self approval must fail')
                RETURNING public_id
                """,
                (admin["id"], campaigns[1][0]),
            )
            self_request_id = cursor.fetchone()[0]
            cursor.execute(
                sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(app_role))
            )
            cursor.execute(
                "SELECT iam.set_request_context(%s, 'admin-self', '')",
                (admin["public_id"],),
            )
            expect_error(
                connection,
                InsufficientPrivilege,
                """
                SELECT iam.admin_decide_campaign_access_request(
                    %s, 'APPROVED', 'must fail',
                    'admin-self', '127.0.0.1'
                )
                """,
                (self_request_id,),
            )
            expect_error(
                connection,
                InsufficientPrivilege,
                "SELECT iam.admin_disable_user(%s, 'must fail', 'admin-self', '127.0.0.1')",
                (admin["public_id"],),
            )
            cursor.execute("RESET ROLE")

            token_hash = "a" * 64
            csrf_hash = "b" * 64
            cursor.execute(
                sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(app_role))
            )
            cursor.execute(
                """
                SELECT * FROM iam.auth_create_browser_session(
                    %s, %s, %s, now() + interval '8 hours'
                )
                """,
                (viewer["public_id"], token_hash, csrf_hash),
            )
            browser_session_id = cursor.fetchone()[0]
            expect_error(
                connection,
                Exception,
                """
                SELECT * FROM iam.auth_create_browser_session(
                    %s, %s, %s, now() + interval '8 hours'
                )
                """,
                (service["public_id"], "c" * 64, "d" * 64),
            )
            assert (
                scalar(
                    cursor,
                    "SELECT count(*) FROM iam.auth_resolve_browser_session(%s)",
                    (token_hash,),
                )
                == 1
            )
            cursor.execute(
                "SELECT iam.set_request_context(%s, 'viewer-logout', '')",
                (viewer["public_id"],),
            )
            assert scalar(
                cursor,
                """
                SELECT iam.auth_revoke_browser_session(
                    %s, 'viewer-logout', '127.0.0.1'
                )
                """,
                (browser_session_id,),
            )
            assert (
                scalar(
                    cursor,
                    "SELECT count(*) FROM iam.auth_resolve_browser_session(%s)",
                    (token_hash,),
                )
                == 0
            )
            cursor.execute("RESET ROLE")
            cursor.execute(
                """
                INSERT INTO iam.auth_session(
                    user_id, token_hash, csrf_token_hash, session_type,
                    created_at, last_seen_at, expires_at
                ) VALUES (
                    %s, %s, %s, 'BROWSER',
                    now() - interval '2 hours', now() - interval '2 hours',
                    now() - interval '1 hour'
                )
                """,
                (viewer["id"], "e" * 64, "f" * 64),
            )
            cursor.execute(
                sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(app_role))
            )
            assert scalar(
                cursor,
                "SELECT count(*) FROM iam.auth_resolve_browser_session(%s)",
                ("e" * 64,),
            ) == 0
            cursor.execute("RESET ROLE")

            cursor.execute(
                "SELECT iam.set_request_context(%s, 'audit-append-only', '')",
                (admin["public_id"],),
            )
            event_id = scalar(
                cursor,
                "SELECT public_id FROM audit.security_event ORDER BY id DESC LIMIT 1",
            )
            cursor.execute(
                sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(app_role))
            )
            expect_error(
                connection,
                InsufficientPrivilege,
                "UPDATE audit.security_event SET outcome='DENIED' WHERE public_id=%s",
                (event_id,),
            )
            expect_error(
                connection,
                InsufficientPrivilege,
                "DELETE FROM audit.security_event WHERE public_id=%s",
                (event_id,),
            )
            cursor.execute("RESET ROLE")

        connection.rollback()
    print("account access verification passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
