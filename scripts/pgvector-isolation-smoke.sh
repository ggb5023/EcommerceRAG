#!/usr/bin/env bash
set -euo pipefail

# Requires a caller-provided restricted DSN; never prints it.
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
check=$(psql "$dsn" -X -v ON_ERROR_STOP=1 -At <<'SQL'
SELECT CASE WHEN EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'vector')
       THEN 'vector_extension=PASS' ELSE 'vector_extension=FAIL' END;
SELECT CASE WHEN EXISTS (SELECT 1 FROM pg_attribute
                         WHERE attrelid = 'chunk'::regclass AND attname = 'embedding')
       THEN 'chunk_embedding_column=PASS' ELSE 'chunk_embedding_column=FAIL' END;
SELECT CASE WHEN EXISTS (SELECT 1 FROM pg_indexes WHERE indexname = 'idx_chunk_embedding')
       THEN 'chunk_embedding_index=PASS' ELSE 'chunk_embedding_index=FAIL' END;
SELECT CASE WHEN EXISTS (SELECT 1 FROM pg_roles WHERE rolname = current_user AND rolsuper = false)
       THEN 'app_non_superuser=PASS' ELSE 'app_non_superuser=FAIL' END;
SQL
)
if grep -q '=FAIL' <<<"$check"; then
  printf '%s\n' "$check"
  exit 4
fi
printf '%s\n' "$check"
psql "$dsn" -X -v ON_ERROR_STOP=1 -f "$repo/tests/pgvector-isolation-smoke.sql"
echo "real_service_acceptance=false"
