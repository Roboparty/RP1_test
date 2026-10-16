#!/usr/bin/env sh
set -eu
cd "$(dirname "$0")/.."
exec scripts/compose.sh stop
