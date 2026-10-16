"""Read migration SQL and substitute validated PostgreSQL role identifiers."""
from __future__ import annotations

import os
import re
from pathlib import Path
from urllib.parse import unquote, urlparse

SQL_ROOT = Path(__file__).resolve().parents[1] / 'sql'
ROLE_PATTERN = re.compile(r'^[A-Za-z_][A-Za-z0-9_$]*$')


def role_identifier(environment_key: str, default: str) -> str:
    value = os.environ.get(environment_key, default)
    if not ROLE_PATTERN.fullmatch(value):
        raise RuntimeError(f'{environment_key} must be a PostgreSQL role identifier')
    # Identifiers cannot be bound as SQL values. Validate, then quote explicitly.
    return '"' + value + '"'


def read_sql(filename: str) -> str:
    if Path(filename).name != filename:
        raise ValueError('SQL migration filename must not contain directories')
    sql = (SQL_ROOT / filename).read_text(encoding='utf-8')
    role_keys = [('RP1_APP_USER', 'rp1_app'), ('RP1_READONLY_USER', 'rp1_readonly')]
    if '__RP1_MIGRATOR_USER__' in sql:
        username = unquote(urlparse(os.environ.get('DATABASE_URL', '')).username or 'rp1_migrator')
        role_keys.append(('RP1_MIGRATOR_USER', username))
    for key, default in role_keys:
        placeholder = f'__{key}__'
        if placeholder in sql:
            sql = sql.replace(placeholder, role_identifier(key, default))
    if '__RP1_' in sql:
        raise RuntimeError('unresolved PostgreSQL role placeholder')
    return sql
