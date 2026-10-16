#!/usr/bin/env python3
"""Disposable local database tests. Reuses local roles without displaying credentials."""
import os
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]
settings = dict(line.split("=", 1) for line in (ROOT / ".env").read_text().splitlines() if line and not line.startswith("#"))
project = settings["COMPOSE_PROJECT_NAME"]
database = "rp1_presence_setup"
container = project + "-postgres-1"


def run(args, **kwargs):
    subprocess.run(args, check=True, **kwargs)


run(["docker", "exec", container, "createdb", "-U", settings["POSTGRES_USER"], database])
environment = os.environ.copy()
admin_url = f"postgresql+psycopg://{settings['POSTGRES_USER']}:{settings['POSTGRES_PASSWORD']}@postgres:5432/{database}"
environment.update({
    "DATABASE_URL": admin_url, "RP1_APP_USER": settings["RP1_APP_USER"],
    "RP1_READONLY_USER": settings["RP1_READONLY_USER"], "IMPORT_LEGACY_HISTORY": "false",
    "HMI_TEST_ADMIN_URL": admin_url, "APP_ENV": "test", "AUTH_MODE": "session",
    "AUTH_COOKIE_SECURE": "false", "PYTHONPATH": "/app", "SEED_ADMIN_USERNAME": "",
})
network = project + "_default"
try:
    run(["docker", "run", "--rm", "--network", network, "-v", str(ROOT / "database") + ":/workspace:ro",
         *sum((["-e", key] for key in ("DATABASE_URL", "RP1_APP_USER", "RP1_READONLY_USER", "IMPORT_LEGACY_HISTORY")), []),
         "rp1-test-platform-migrate:1.0.1"], env=environment)
    environment["DATABASE_URL"] = f"postgresql+psycopg://{settings['RP1_APP_USER']}:{settings['RP1_APP_PASSWORD']}@postgres:5432/{database}"
    run(["docker", "run", "--rm", "--user", "0", "--network", network, "-v", str(ROOT / "service") + ":/app:ro", "-v", str(ROOT / "database") + ":/database:ro",
         *sum((["-e", key] for key in ("DATABASE_URL", "HMI_TEST_ADMIN_URL", "APP_ENV", "AUTH_MODE", "AUTH_COOKIE_SECURE", "PYTHONPATH", "SEED_ADMIN_USERNAME")), []),
         os.environ.get("RP1_QA_IMAGE", "rp1-pc3-qa-tools:local"), "sh", "-c",
         "python -m pytest -p no:cacheprovider -c pytest.ini tests -m 'not integration' -q && python -m pytest -p no:cacheprovider -c pytest.ini tests/test_setup_integration.py tests/test_hmi_presence_integration.py -q"], env=environment)
finally:
    run(["docker", "exec", container, "dropdb", "-U", settings["POSTGRES_USER"], database])
