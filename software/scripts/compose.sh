#!/usr/bin/env bash
# The delivery's .env is authoritative, even on hosts running another RP1 installation.
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
test -f .env || { echo 'Run python3 scripts/configure.py first.' >&2; exit 1; }
unset_args=(-u COMPOSE_FILE -u COMPOSE_PROJECT_NAME -u COMPOSE_PROFILES)
while IFS='=' read -r key ignored; do
  [[ $key =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] && unset_args+=(-u "$key")
done < .env
compose_files=(-f compose.yaml)
# Preserve site-specific bindings and mounts during upgrades and maintenance.
if [[ -f compose.override.yml ]]; then
  compose_files+=(-f compose.override.yml)
fi
exec env "${unset_args[@]}" docker compose --env-file .env "${compose_files[@]}" "$@"
