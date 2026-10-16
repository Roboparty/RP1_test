#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
test -f .env || { echo 'Run python3 scripts/configure.py first.' >&2; exit 1; }
scripts/compose.sh config --quiet
scripts/compose.sh build migrate api worker archive-worker web
scripts/compose.sh up -d --wait --wait-timeout 120 postgres
scripts/compose.sh run --rm --no-deps migrate
scripts/compose.sh up -d --no-deps --wait --wait-timeout 300 api worker archive-worker web
python3 scripts/verify.py
