"""Check worker process and database reachability, without changing business data."""
from pathlib import Path
from sqlalchemy import text
from app.core.db import engine

assert b'app.services.worker' in Path('/proc/1/cmdline').read_bytes(), 'Unexpected worker process'
with engine.connect() as connection:
    assert connection.execute(text('SELECT 1')).scalar_one() == 1
print('Worker process and PostgreSQL reachable')
