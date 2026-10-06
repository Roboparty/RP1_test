#!/usr/bin/env sh
set -eu
cd "$(dirname "$0")/.."
python3 scripts/init_env.py
docker compose config --quiet
docker compose up --build -d
