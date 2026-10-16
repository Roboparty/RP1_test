#!/usr/bin/env python3
"""Validate the specifically authorized legacy execution metadata, without writes."""
import argparse
from collections import Counter
import csv
import hashlib
import io
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'database'))
from imports.execution_csv import validate_rows  # noqa: E402

EXPECTED_ASSETS = {'RP1.3-SARM-001','RP1.3-SARM-002','RP1.3-SARM-003','RP1.3-SLEG-001','RP1.3-SLEG-002','RP1.3-UPPER-001','RP1.3-LOWER-001'}


def preflight(path: Path, expected_sha256: str):
    if not re.fullmatch(r'[a-fA-F0-9]{64}', expected_sha256):
        raise ValueError('invalid checksum')
    if path.stat().st_size > 16 * 1024 * 1024:
        raise ValueError('unexpected file size')
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if digest != expected_sha256.lower():
        raise ValueError('source checksum mismatch')
    source_text = raw.decode('utf-8-sig')
    if 'SHOWCASE' in source_text.upper():
        raise ValueError('showcase content rejected')
    reader = csv.DictReader(io.StringIO(source_text, newline=''))
    rows = validate_rows(reader.fieldnames, reader)
    if {row['asset_code'] for row in rows} != EXPECTED_ASSETS:
        raise ValueError('unexpected sample set')
    runtimes = sum(row['active_seconds'] is not None for row in rows)
    if runtimes != 124:
        raise ValueError('unexpected runtime count')
    return {'sha256': digest, 'executions': len(rows), 'samples': 7, 'parts': 4,
            'legacy_reported_intervals': runtimes, 'source_statuses': dict(Counter(row['source_status'] for row in rows)),
            'warnings': dict(Counter(w['code'] for row in rows for w in row['normalization_warnings'])),
            'writes_performed': False, 'isolated_import_still_required': True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('csv', type=Path)
    parser.add_argument('--sha256', required=True, help='checksum independently confirmed on computer 1')
    args = parser.parse_args()
    try:
        summary = preflight(args.csv, args.sha256)
    except (OSError, ValueError, UnicodeError, csv.Error, KeyError, TypeError, AttributeError):
        print('BLOCKED: source file missing or source checksum, format, counts, normalization or SHOWCASE exclusion did not pass. No database writes.', file=sys.stderr)
        return 2
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
