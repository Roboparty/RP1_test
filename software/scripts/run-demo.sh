#!/usr/bin/env sh
set -eu
cd "$(dirname "$0")/../examples/telemetry-demo"
exec sh scripts/run-demo.sh "$@"
