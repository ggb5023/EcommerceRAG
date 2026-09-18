#!/usr/bin/env bash
set -euo pipefail
umask 077
repo=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
suffix="$(date -u +%Y%m%d%H%M%S)_$$"
testdb="ecr_verify_$suffix"
restoredb="ecr_restore_$suffix"
tempdir=$(mktemp -d /tmp/ecr-verify.XXXXXX)
cleanup() {
  docker exec rag-postgres dropdb -U postgres --if-exists "$restoredb" >/dev/null
  docker exec rag-postgres dropdb -U postgres --if-exists "$testdb" >/dev/null
  echo "Removed only isolated verification databases; dump retained at $tempdir"
}
trap cleanup EXIT
docker exec rag-postgres createdb -U postgres "$testdb"
docker exec -i rag-postgres psql -X -v ON_ERROR_STOP=1 -U postgres -d "$testdb" < "$repo/docker/db/init/01_extensions.sql"
docker exec -i rag-postgres psql -X -v ON_ERROR_STOP=1 -U postgres -d "$testdb" < "$repo/sql/migrations/0001_init.sql"
docker exec -i rag-postgres psql -X -v ON_ERROR_STOP=1 -U postgres -d "$testdb" < "$repo/tests/schema-smoke.sql"
docker restart rag-postgres >/dev/null
for _ in $(seq 1 30); do
  if docker exec rag-postgres pg_isready -U postgres -d "$testdb" >/dev/null 2>&1; then break; fi
  sleep 1
done
[[ "$(docker exec rag-postgres psql -X -U postgres -d "$testdb" -Atc 'SELECT count(*) FROM chunk')" == 1 ]]
echo 'PASS PostgreSQL restart preserves fixture data'
docker exec rag-postgres pg_dump -U postgres -d "$testdb" -Fc > "$tempdir/fixture.dump"
docker exec rag-postgres createdb -U postgres "$restoredb"
docker exec -i rag-postgres pg_restore -U postgres -d "$restoredb" --exit-on-error < "$tempdir/fixture.dump"
[[ "$(docker exec rag-postgres psql -X -U postgres -d "$restoredb" -Atc 'SELECT count(*) FROM chunk')" == 1 ]]
echo 'PASS 18-table schema, HNSW, tenant constraints, idempotency and fixture restore'
bash "$repo/scripts/backup-db.sh"
latest=$(find /data/backups -maxdepth 1 -name 'rag-????????T??????Z.dump' | sort | tail -1)
sha256sum -c "$latest.sha256"
docker exec rag-postgres dropdb -U postgres "$restoredb"
docker exec rag-postgres createdb -U postgres "$restoredb"
docker exec -i rag-postgres pg_restore -U postgres -d "$restoredb" --exit-on-error < "$latest"
[[ "$(docker exec rag-postgres psql -X -U postgres -d "$restoredb" -Atc "SELECT count(*) FROM pg_extension WHERE extname IN ('vector','pg_trgm')")" == 2 ]]
echo 'PASS actual rag backup restore'
