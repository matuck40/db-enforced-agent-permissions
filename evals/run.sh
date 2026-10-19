#!/usr/bin/env bash
# Run the eval runner against a fresh throwaway Postgres from docker-compose.yml.
# All arguments go to `python -m evals.run`. Exit code is the runner's.
#   args=(--schema schema.sql); for m in migrations/*.sql; do args+=(--schema "$m"); done
#   evals/run.sh --mode offline --config all "${args[@]}" --out evals/reports/offline-latest.md
# --schema repeats: the files load in the order given, so pass schema.sql and
# then every migrations/*.sql in name order (the glob sorts). --schema
# schema.sql alone is the schema before the migrations.
set -uo pipefail
cd "$(dirname "$0")/.."

cleanup() { docker compose down -v >/dev/null 2>&1; }
trap cleanup EXIT

docker compose down -v >/dev/null 2>&1
docker compose up -d --wait || exit 1
EVAL_ADMIN_DATABASE_URL=postgresql://postgres@127.0.0.1:55432/app \
  uv run --project agent python -m evals.run "$@"
code=$?
exit "$code"
