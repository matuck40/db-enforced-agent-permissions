#!/usr/bin/env bash
# Starts a fresh Postgres, loads the schema, and runs every role test as that
# role. Needs only Docker. Exit code is 0 only if every file passed.
#
#   ./run-tests.sh              run and tear down
#   KEEP_DB=1 ./run-tests.sh    leave the database up afterwards (port 55432)
#
# With EXTERNAL_DB=1 it skips Docker and runs against the database that the
# PG* variables point to, through the local psql (this is how CI uses a service
# container). An explicit switch, not PGHOST alone, because many machines export
# PGHOST for their own database. That database must be fresh, named app, and
# trust its logins: the schema creates cluster-wide roles and the role logins
# have no password.
#
#   EXTERNAL_DB=1 PGHOST=localhost PGPORT=5432 ./run-tests.sh
set -euo pipefail
cd "$(dirname "$0")"

if [ "${EXTERNAL_DB:-0}" = 1 ]; then
  echo "Using the database at ${PGHOST:-local socket}:${PGPORT:-5432}"
  psql_as() {
    psql -X -q -v ON_ERROR_STOP=1 -U "$1" -d app
  }
else
  psql_as() {
    docker compose exec -T db psql -X -q -v ON_ERROR_STOP=1 -U "$1" -d app
  }
  docker compose down -v --remove-orphans >/dev/null 2>&1 || true
  docker compose up -d --wait --quiet-pull >/dev/null 2>&1
fi

# Setup is the only step that runs as the bootstrap superuser.
psql_as postgres < schema.sql
psql_as postgres < tests/setup.sql

failed=0
for file in tests/[0-9]*_*.sql; do
  role=$(sed -n 's/^-- role: *//p' "$file" | head -n 1)
  if [ -z "$role" ] || [ "$role" = postgres ]; then
    echo "FAILED: $file must declare a non-superuser role"; failed=1; continue
  fi
  echo "== $file (connected as $role)"
  if output=$(psql_as "$role" < "$file" 2>&1); then status=0; else status=$?; fi
  echo "$output" | sed '/^$/d'
  # A clean exit, the file's own final line, and proof of who was connected.
  if [ "$status" -ne 0 ] || ! grep -q '^OK: ' <<<"$output" \
     || ! grep -q "^ok  *connected as $role, not a superuser" <<<"$output"; then
    echo "FAILED: $file"
    failed=1
  fi
  echo
done

if [ "${EXTERNAL_DB:-0}" != 1 ] && [ "${KEEP_DB:-0}" != 1 ]; then
  docker compose down -v >/dev/null 2>&1
fi

if [ "$failed" -eq 0 ]; then echo "ALL PASSED"; else echo "SOME TESTS FAILED"; fi
exit "$failed"
