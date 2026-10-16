#!/usr/bin/env python3
"""Run approval regressions in a NEW PostgreSQL container; never read site .env."""
import os
from pathlib import Path
import secrets
import subprocess
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]


def main():
    name = "rp1-approval-qa-" + uuid4().hex[:12]
    env = os.environ.copy()
    env.update(POSTGRES_USER="postgres", POSTGRES_DB="rp1_presence_approval",
               POSTGRES_PASSWORD=secrets.token_hex(24), RP1_APP_USER="rp1_app",
               RP1_APP_PASSWORD=secrets.token_hex(24), RP1_READONLY_USER="rp1_readonly",
               RP1_READONLY_PASSWORD=secrets.token_hex(24))

    def run(args, **kwargs):
        return subprocess.run(args, env=env, check=True, **kwargs)

    started = False
    try:
        run(["docker", "run", "-d", "--name", name, "--label", "rp1.disposable=approval-test",
             *sum((["-e", key] for key in ("POSTGRES_USER", "POSTGRES_DB", "POSTGRES_PASSWORD", "RP1_APP_USER", "RP1_APP_PASSWORD", "RP1_READONLY_USER", "RP1_READONLY_PASSWORD")), []),
             "-v", str(ROOT / "database/docker-entrypoint-initdb.d") + ":/docker-entrypoint-initdb.d:ro",
             "postgres:16-alpine"], stdout=subprocess.DEVNULL)
        started = True
        for _ in range(60):
            ready = subprocess.run(["docker", "exec", name, "pg_isready", "-U", "postgres", "-d", env["POSTGRES_DB"]], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if ready.returncode == 0:
                # Initialization may use a temporary socket-only server.
                probe = subprocess.run(["docker", "exec", name, "pg_isready", "-h", "127.0.0.1", "-U", "postgres"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                if probe.returncode == 0:
                    break
            time.sleep(.5)
        else:
            raise RuntimeError("Disposable PostgreSQL did not become ready")
        env.update(DATABASE_URL=f"postgresql+psycopg://postgres:{env['POSTGRES_PASSWORD']}@127.0.0.1:5432/{env['POSTGRES_DB']}", IMPORT_LEGACY_HISTORY="false")
        print("Migrating disposable database only", flush=True)
        run(["docker", "run", "--rm", "--network", "container:" + name, "-v", str(ROOT / "database") + ":/workspace:ro",
             *sum((["-e", key] for key in ("DATABASE_URL", "RP1_APP_USER", "RP1_READONLY_USER", "IMPORT_LEGACY_HISTORY")), []), "rp1-test-platform-migrate:1.0.1"])
        env["HMI_TEST_ADMIN_URL"] = env["DATABASE_URL"]
        env.update(DATABASE_URL=f"postgresql+psycopg://{env['RP1_APP_USER']}:{env['RP1_APP_PASSWORD']}@127.0.0.1:5432/{env['POSTGRES_DB']}",
                   APP_ENV="test", AUTH_MODE="session", AUTH_COOKIE_SECURE="false", PYTHONPATH="/app", SEED_ADMIN_USERNAME="")
        print("Testing approval API, transactions and permissions", flush=True)
        command = ["docker", "run", "--rm", "--user", "0", "--network", "container:" + name,
             "-v", str(ROOT / "service") + ":/app:ro", "-v", str(ROOT / "database") + ":/database:ro",
             *sum((["-e", key] for key in ("DATABASE_URL", "HMI_TEST_ADMIN_URL", "APP_ENV", "AUTH_MODE", "AUTH_COOKIE_SECURE", "PYTHONPATH", "SEED_ADMIN_USERNAME")), []),
             "rp1-pc3-qa-tools:local", "python", "-m", "pytest", "-p", "no:cacheprovider", "-c", "pytest.ini"]
        run([*command, "tests", "-m", "not integration", "-q", "--disable-warnings"])
        # Older test_integration_db cases require a separate SHOWCASE/history
        # fixture and intentionally write formal facts directly. This runner is
        # an empty-site approval test, never a SHOWCASE/production replay.
        run([*command, "tests/test_setup_integration.py", "tests/test_hmi_presence_integration.py",
             "tests/test_submissions_integration.py", "-q", "--disable-warnings"])
    finally:
        if started:
            subprocess.run(["docker", "rm", "-f", "-v", name], check=True, stdout=subprocess.DEVNULL)
            print("Disposable database removed; production was not used", flush=True)


if __name__ == "__main__":
    main()
