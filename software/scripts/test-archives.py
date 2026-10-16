#!/usr/bin/env python3
"""Archive/approval acceptance in disposable storage and PostgreSQL, never site .env."""
import argparse
import os
from pathlib import Path
import secrets
import subprocess
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tools-image", default="rp1-archive-qa:local")
    parser.add_argument("--migration-image", help="Optional migration runtime; defaults to the tools image")
    args = parser.parse_args()
    name = "rp1-archives-qa-" + uuid4().hex[:12]
    volume = name + "-files"
    env = os.environ.copy()
    env.update(POSTGRES_USER="postgres", POSTGRES_DB="rp1_presence_archives",
               POSTGRES_PASSWORD=secrets.token_hex(24), RP1_APP_USER="rp1_app",
               RP1_APP_PASSWORD=secrets.token_hex(24), RP1_READONLY_USER="rp1_readonly",
               RP1_READONLY_PASSWORD=secrets.token_hex(24))

    def run(command, **kwargs):
        return subprocess.run(command, env=env, check=True, **kwargs)

    def flags(keys):
        return [item for key in keys for item in ("-e", key)]

    database_created = volume_created = False
    try:
        run(["docker", "volume", "create", "--label", "rp1.disposable=archive-test", volume], stdout=subprocess.DEVNULL)
        volume_created = True
        run(["docker", "run", "-d", "--name", name, "--label", "rp1.disposable=archive-test",
             *flags(("POSTGRES_USER", "POSTGRES_DB", "POSTGRES_PASSWORD", "RP1_APP_USER", "RP1_APP_PASSWORD", "RP1_READONLY_USER", "RP1_READONLY_PASSWORD")),
             "-v", str(ROOT / "database/docker-entrypoint-initdb.d") + ":/docker-entrypoint-initdb.d:ro",
             "postgres:16-alpine"], stdout=subprocess.DEVNULL)
        database_created = True
        for _ in range(90):
            ready = subprocess.run(["docker", "exec", name, "pg_isready", "-h", "127.0.0.1", "-U", "postgres"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if ready.returncode == 0:
                break
            time.sleep(.5)
        else:
            raise RuntimeError("Disposable database did not become ready")
        env.update(DATABASE_URL=f"postgresql+psycopg://postgres:{env['POSTGRES_PASSWORD']}@127.0.0.1:5432/{env['POSTGRES_DB']}", IMPORT_LEGACY_HISTORY="false")
        print("Migrating disposable archive database", flush=True)
        run(["docker", "run", "--rm", "--network", "container:" + name, "--workdir", "/workspace",
             "-v", str(ROOT / "database") + ":/workspace:ro",
             *flags(("DATABASE_URL", "RP1_APP_USER", "RP1_READONLY_USER", "IMPORT_LEGACY_HISTORY")),
             args.migration_image or args.tools_image, "alembic", "upgrade", "head"])
        env["HMI_TEST_ADMIN_URL"] = env["DATABASE_URL"]
        env.update(DATABASE_URL=f"postgresql+psycopg://{env['RP1_APP_USER']}:{env['RP1_APP_PASSWORD']}@127.0.0.1:5432/{env['POSTGRES_DB']}",
                   APP_ENV="test", AUTH_MODE="session", AUTH_COOKIE_SECURE="false", PYTHONPATH="/app:/hmi", SEED_ADMIN_USERNAME="", ARCHIVE_ROOT="/archive")
        command = ["docker", "run", "--rm", "--user", "0", "--network", "container:" + name,
                   "-v", str(ROOT / "service") + ":/app:ro", "-v", str(ROOT / "database") + ":/database:ro",
                   "-v", str(ROOT / "host") + ":/hmi:ro",
                   "-v", volume + ":/archive", *flags(("DATABASE_URL", "HMI_TEST_ADMIN_URL", "APP_ENV", "AUTH_MODE", "AUTH_COOKIE_SECURE", "PYTHONPATH", "SEED_ADMIN_USERNAME", "ARCHIVE_ROOT")),
                   args.tools_image, "python", "-m", "pytest", "-p", "no:cacheprovider", "-c", "pytest.ini"]
        run([*command, "tests", "-m", "not integration", "-q", "--disable-warnings"])
        tests = ["tests/test_setup_integration.py", "tests/test_hmi_presence_integration.py", "tests/test_submissions_integration.py"]
        tests += ["tests/" + path.name for path in sorted((ROOT / "service/tests").glob("test_*archive*integration.py"))]
        print("Testing archive integrity, roles, curves and publication", flush=True)
        run([*command, *tests, "-q", "--disable-warnings"])
    finally:
        if database_created:
            subprocess.run(["docker", "rm", "-f", "-v", name], check=True, stdout=subprocess.DEVNULL)
        if volume_created:
            subprocess.run(["docker", "volume", "rm", volume], check=True, stdout=subprocess.DEVNULL)
        print("Disposable archive database/storage removed; site data was not used", flush=True)


if __name__ == "__main__":
    main()
