#!/usr/bin/env bash
set -euo pipefail
repo=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
if [[ -z "${M1_MIGRATION_DATABASE_URL:-}" || -z "${M1_APP_DATABASE_URL:-}" ]]; then
  echo "Set M1_MIGRATION_DATABASE_URL and M1_APP_DATABASE_URL for an isolated test database." >&2
  exit 2
fi

admin_info=$(psql "$M1_MIGRATION_DATABASE_URL" -X -A -t -F '|' -v ON_ERROR_STOP=1 \
  -c "SELECT current_database(), current_user")
app_info=$(psql "$M1_APP_DATABASE_URL" -X -A -t -F '|' -v ON_ERROR_STOP=1 \
  -c "SELECT current_database(), current_user, r.rolsuper FROM pg_roles r WHERE r.rolname=current_user")
IFS='|' read -r db_name migration_user <<<"$admin_info"
IFS='|' read -r app_db app_user app_superuser <<<"$app_info"
if [[ "$db_name" != "$app_db" || "$db_name" != *m1_test* ]]; then
  echo "Both DSNs must target the same dedicated database with 'm1_test' in its name." >&2
  exit 2
fi
if [[ "$migration_user" == "$app_user" || "$app_superuser" != f ]]; then
  echo "Migration and application accounts must differ; the application account must not be superuser." >&2
  exit 2
fi

has_migrations=$(psql "$M1_MIGRATION_DATABASE_URL" -X -A -t -v ON_ERROR_STOP=1 \
  -c "SELECT to_regclass('public.schema_migration') IS NOT NULL")
if [[ "$has_migrations" != t ]]; then
  tables=$(psql "$M1_MIGRATION_DATABASE_URL" -X -A -t -v ON_ERROR_STOP=1 \
    -c "SELECT count(*) FROM information_schema.tables WHERE table_schema='public' AND table_type='BASE TABLE'")
  if [[ "$tables" != 0 ]]; then
    echo "Refusing to initialize a non-empty database without schema_migration." >&2
    exit 2
  fi
fi

for migration in "$repo"/sql/migrations/*.sql; do
  version=$(basename "$migration" .sql)
  version=${version:0:4}
  applied=$(psql "$M1_MIGRATION_DATABASE_URL" -X -A -t -v ON_ERROR_STOP=1 \
    -c "SELECT EXISTS (SELECT 1 FROM schema_migration WHERE version='$version')" 2>/dev/null || true)
  if [[ "$applied" != t ]]; then
    echo "Applying $version to $db_name"
    psql "$M1_MIGRATION_DATABASE_URL" -X -v ON_ERROR_STOP=1 -f "$migration"
  fi
done

psql "$M1_APP_DATABASE_URL" -X -v ON_ERROR_STOP=1 -f "$repo/tests/m1-schema-smoke.sql"
echo "PASS M1 isolated migrations and application-role schema smoke ($db_name)"
