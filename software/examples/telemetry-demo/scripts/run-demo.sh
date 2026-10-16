#!/usr/bin/env sh
set -eu
cd "$(dirname "$0")/.."
python3 scripts/init_env.py
docker compose --profile demo run --rm bootstrap-demo
docker compose --profile demo run --build --rm demo "$@"
