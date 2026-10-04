#!/usr/bin/env bash
set -euo pipefail

repo="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
dsn="${M1_APP_DATABASE_URL:-}"
if [[ -z "$dsn" ]]; then
  echo "NOT_RUN/CONFIG_BLOCKED M1_APP_DATABASE_URL is not set"
  exit 3
fi
if ! command -v psql >/dev/null 2>&1; then
  echo "NOT_RUN/CONFIG_BLOCKED psql is unavailable"
  exit 3
fi
psql "$dsn" -X -v ON_ERROR_STOP=1 -f "$repo/tests/ingest-worker-runtime-smoke.sql"
echo "real_service_acceptance=false"
