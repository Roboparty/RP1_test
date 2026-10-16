"""Check the archive processor and migrated database without device I/O."""
from pathlib import Path

from sqlalchemy import text

from app.core.db import engine

assert b"app.services.archive_worker" in Path("/proc/1/cmdline").read_bytes(), "Unexpected archive worker process"
with engine.connect() as connection:
    assert connection.execute(text("SELECT to_regclass('integration.execution_archive') IS NOT NULL")).scalar_one()
print("Archive processor and migrated PostgreSQL reachable")
