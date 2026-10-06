#!/usr/bin/env sh
set -eu
: "${DATABASE_URL:?Set the new empty database URL}"
: "${1:?Usage: restore.sh /path/to/backup.dump}"
# No --clean or DROP: restore only to an explicitly supplied empty database.
pg_restore --exit-on-error --no-owner --dbname="$DATABASE_URL" "$1"
