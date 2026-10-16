#!/usr/bin/env sh
set -eu
cd "$(dirname "$0")/.."
if [ ! -f .env ]; then
  python3 scripts/configure.py
fi
exec scripts/up.sh
