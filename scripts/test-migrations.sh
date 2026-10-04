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

mapfile -t migration_files < <(find "$repo/sql/migrations" -maxdepth 1 -type f -name '[0-9][0-9][0-9][0-9]_*.sql' -printf '%f\n' | sort)
expected=1
known_versions_sql=
for filename in "${migration_files[@]}"; do
  version=${filename:0:4}
  [[ "$version" == "$(printf '%04d' "$expected")" ]] || { echo "FAIL migration version order" >&2; exit 2; }
  known_versions_sql+="'$version',"
  expected=$((expected + 1))
done
known_versions=${known_versions_sql%,}
if [[ "$has_migrations" == t ]]; then
  unknown=$(psql "$M1_MIGRATION_DATABASE_URL" -X -A -t -v ON_ERROR_STOP=1 \
    -c "SELECT count(*) FROM schema_migration WHERE version NOT IN ($known_versions)")
  [[ "$unknown" == 0 ]] || { echo "FAIL unknown future migration version recorded" >&2; exit 2; }
fi
has_checksum_column=f
if [[ "$has_migrations" == t ]]; then
  has_checksum_column=$(psql "$M1_MIGRATION_DATABASE_URL" -X -A -t -v ON_ERROR_STOP=1 \
    -c "SELECT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='schema_migration' AND column_name='checksum_sha256')")
fi
legacy_unverified=0
for filename in "${migration_files[@]}"; do
  migration="$repo/sql/migrations/$filename"; version=${filename:0:4}; applied=f
  if [[ "$has_checksum_column" == t ]]; then
    row=$(psql "$M1_MIGRATION_DATABASE_URL" -X -A -t -F '|' -v ON_ERROR_STOP=1 \
      -c "SELECT checksum_sha256, applied_at FROM schema_migration WHERE version='$version'")
    if [[ -n "$row" ]]; then
      applied=t; IFS='|' read -r recorded_checksum _ <<<"$row"
      actual_checksum=$(sha256sum "$migration" | awk '{print $1}')
      if [[ -z "$recorded_checksum" ]]; then
        legacy_unverified=1; echo "LEGACY_UNVERIFIED $version checksum is NULL"
      elif [[ "$recorded_checksum" != "$actual_checksum" ]]; then
        echo "FAIL checksum mismatch for $version" >&2; exit 1
      fi
    fi
  else
    if [[ "$has_migrations" == t ]]; then
      applied=$(psql "$M1_MIGRATION_DATABASE_URL" -X -A -t -v ON_ERROR_STOP=1 \
        -c "SELECT EXISTS (SELECT 1 FROM schema_migration WHERE version='$version')")
    fi
    if [[ "$applied" == t ]]; then legacy_unverified=1; echo "LEGACY_UNVERIFIED $version checksum column is not available"; fi
  fi
  if [[ "$applied" != t ]]; then
    echo "Applying $version to $db_name"
    checksum=$(sha256sum "$migration" | awk '{print $1}')
    if [[ "$has_checksum_column" == t || "$version" == 0004 ]]; then
      psql "$M1_MIGRATION_DATABASE_URL" -X -v ON_ERROR_STOP=1 -v migration_checksum="$checksum" -f "$migration"
      has_checksum_column=t
    else
      psql "$M1_MIGRATION_DATABASE_URL" -X -v ON_ERROR_STOP=1 -f "$migration"
    fi
    has_migrations=t
  fi
done

if [[ "$has_checksum_column" == t ]]; then
  legacy_count=$(psql "$M1_MIGRATION_DATABASE_URL" -X -A -t -v ON_ERROR_STOP=1 \
    -c "SELECT count(*) FROM schema_migration WHERE checksum_sha256 IS NULL")
  if [[ "$legacy_count" != 0 ]]; then legacy_unverified=1; fi
fi

psql "$M1_APP_DATABASE_URL" -X -v ON_ERROR_STOP=1 -f "$repo/tests/m1-schema-smoke.sql"
psql "$M1_APP_DATABASE_URL" -X -v ON_ERROR_STOP=1 -f "$repo/tests/admin-control-plane-smoke.sql"
psql "$M1_MIGRATION_DATABASE_URL" -X -v ON_ERROR_STOP=1 -f "$repo/tests/m1-auth-constraints.sql"
if [[ "$legacy_unverified" == 1 ]]; then
  echo "LEGACY_UNVERIFIED M1 isolated migrations and application-role schema smoke ($db_name)"
else
  echo "PASS M1 isolated migrations and application-role schema smoke ($db_name)"
fi
