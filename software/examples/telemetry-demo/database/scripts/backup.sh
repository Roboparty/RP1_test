#!/usr/bin/env sh
set -eu
# DATABASE_URL uses libpq form: postgresql:// (without +psycopg).
: "${DATABASE_URL:?Set an isolated database URL}"
: "${1:?Usage: backup.sh /path/to/backup.dump}"
umask 077
pg_dump --format=custom --file="$1" "$DATABASE_URL"
