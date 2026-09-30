#!/usr/bin/env bash
set -euo pipefail

repo=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
printf 'M1 environment check\n'
printf 'repo=%s\n' "$repo"
printf 'git=%s\n' "$(git -C "$repo" rev-parse --short HEAD)"

report() { printf '%-18s %-9s %s\n' "$1" "$2" "$3"; }

if command -v psql >/dev/null 2>&1; then
  report postgres PASS "psql available"
else
  report postgres BLOCKED "psql is unavailable"
fi

if [[ -n "${M1_MIGRATION_DATABASE_URL:-}" && -n "${M1_APP_DATABASE_URL:-}" ]]; then
  report migration-config READY "both isolated database DSNs are set"
  if bash "$repo/scripts/test-migrations.sh"; then
    report migrations PASS "isolated migration and schema smoke passed"
  else
    report migrations FAIL "test-migrations.sh failed"
  fi
else
  report migration-config NOT_RUN "set M1_MIGRATION_DATABASE_URL and M1_APP_DATABASE_URL"
  report migrations NOT_RUN "no database mutation attempted"
fi

if command -v docker >/dev/null 2>&1; then
  containers=$(docker ps --format '{{.Names}}' 2>/dev/null || true)
  if grep -qx 'rag-redis' <<<"$containers"; then
    if bash "$repo/scripts/verify-redis-restart.sh"; then
      report redis PASS "AOF restart persistence check passed"
    else
      report redis FAIL "Redis container exists but verification failed"
    fi
  else
    report redis NOT_RUN "rag-redis container is not running"
  fi
else
  report redis BLOCKED "docker is unavailable"
fi

browser=''
for candidate in chromium chromium-browser google-chrome; do
  if command -v "$candidate" >/dev/null 2>&1; then browser=$candidate; break; fi
done
if [[ -n "$browser" ]]; then
  report browser READY "$browser available; run the four-viewport manual/Playwright check"
else
  report browser NOT_RUN "Chromium/Playwright runtime unavailable"
fi

if [[ -d "$repo/eval" ]]; then
  report eval PASS "evaluation source directory present"
else
  report eval NOT_RUN "evaluation source directory absent"
fi

if bash "$repo/scripts/check.sh" >/tmp/ecommerce-rag-m1-check.log 2>&1; then
  report foundation PASS "scripts/check.sh passed"
else
  report foundation FAIL "scripts/check.sh failed; see /tmp/ecommerce-rag-m1-check.log"
fi
