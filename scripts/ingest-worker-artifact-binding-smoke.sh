#!/usr/bin/env bash
set -euo pipefail

repo=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
cd "$repo"

env_file=${SYNTHETIC_REVIEW_ENV:-/etc/ecommerce-rag/synthetic-review.env}
report_path=${ARTIFACT_BINDING_REPORT:-/var/lib/ecommerce-rag/real-docs/reports/worker-artifact-binding-current.json}
grpc_addr=${ARTIFACT_BINDING_GRPC_ADDR:-127.0.0.1:50053}
admin_addr=${ARTIFACT_BINDING_ADMIN_HTTP_ADDR:-127.0.0.1:8084}
member_addr=${ARTIFACT_BINDING_MEMBER_HTTP_ADDR:-127.0.0.1:8085}

if [[ ! -r "$env_file" ]]; then
  printf 'NOT_RUN/CONFIG_BLOCKED synthetic review env is not readable\n' >&2
  exit 3
fi
if ! command -v psql >/dev/null 2>&1 || ! command -v grpcurl >/dev/null 2>&1; then
  printf 'NOT_RUN/CONFIG_BLOCKED psql and grpcurl are required\n' >&2
  exit 3
fi

set -a
# The path is operator-controlled and root-owned; secrets are never printed.
# shellcheck disable=SC1090
source "$env_file"
set +a
: "${PGHOST:?PGHOST is required}"
: "${PGPORT:?PGPORT is required}"
: "${PGDATABASE:?PGDATABASE is required}"
: "${PGUSER:?PGUSER is required}"
: "${PGPASSWORD:?PGPASSWORD is required}"
: "${SYNTHETIC_MANIFEST:?SYNTHETIC_MANIFEST is required}"

case "$grpc_addr $admin_addr $member_addr" in
  *0.0.0.0*|*\[*|*localhost*)
    printf 'NOT_RUN unsafe binding address; loopback is required\n' >&2
    exit 3
    ;;
esac

runtime_root=$(mktemp -d /tmp/ecr-artifact-binding.XXXXXX)
bundle_root="$runtime_root/bundles"
mkdir -m 700 "$bundle_root"
report_dir=$(dirname -- "$report_path")
mkdir -p "$report_dir"
chmod 700 "$report_dir"
gateway="$runtime_root/gateway"
python_log="$runtime_root/python.log"
admin_log="$runtime_root/admin.log"
member_log="$runtime_root/member.log"
admin_report="$runtime_root/admin-report.json"

py_pid=
admin_pid=
member_pid=
cleanup() {
  for pid in "$admin_pid" "$member_pid" "$py_pid"; do
    if [[ -n "$pid" ]]; then
      kill "$pid" 2>/dev/null || true
    fi
  done
  for pid in "$admin_pid" "$member_pid" "$py_pid"; do
    if [[ -n "$pid" ]]; then
      wait "$pid" 2>/dev/null || true
    fi
  done
  rm -rf -- "$runtime_root"
}
trap cleanup EXIT

before_versions=$(PGPASSWORD="$PGPASSWORD" psql -X -Atqc \
  "SELECT count(*) FROM document_version WHERE quality_json ? 'artifact_bundle'")
before_chunks=$(PGPASSWORD="$PGPASSWORD" psql -X -Atqc \
  "SELECT count(*) FROM chunk WHERE source_object_key LIKE '%/raw/%' AND parsed_object_key LIKE '%/parsed/%'")

(cd go && go build -p 2 -o "$gateway" ./cmd/server)

(cd python && exec env \
  APP_ENV="$APP_ENV" \
  GRPC_ADDR="$grpc_addr" \
  RAG_PROFILE=synthetic_import_mock \
  SYNTHETIC_MANIFEST="$SYNTHETIC_MANIFEST" \
  INGEST_ARTIFACT_BUNDLE_ROOT="$bundle_root" \
  AUTHORITY_HTTP_BASE="http://$admin_addr" \
  MOCK_STREAM_PACING_MS=0 \
  .venv/bin/python -m app.server) >"$python_log" 2>&1 &
py_pid=$!

grpc_ready=0
for _ in $(seq 1 60); do
  if grpcurl -plaintext -d '{}' "$grpc_addr" grpc.health.v1.Health/Check >/dev/null 2>&1; then
    grpc_ready=1
    break
  fi
  sleep 1
done
if [[ "$grpc_ready" != 1 ]]; then
  printf 'NOT_RUN synthetic gRPC health did not become ready\n' >&2
  exit 1
fi

start_gateway() {
  local user_id=$1
  local http_addr=$2
  local log_path=$3
  env \
    APP_ENV="$APP_ENV" \
    PGHOST="$PGHOST" PGPORT="$PGPORT" PGDATABASE="$PGDATABASE" PGUSER="$PGUSER" PGPASSWORD="$PGPASSWORD" \
    RAG_GRPC_ADDR="$grpc_addr" RAG_PROFILE=synthetic_import_mock \
    SYNTHETIC_MANIFEST="$SYNTHETIC_MANIFEST" SYNTHETIC_USER_ID="$user_id" \
    SYNTHETIC_SHOP_ID=demo-shop-east SYNTHETIC_BUSINESS_DATE="${SYNTHETIC_BUSINESS_DATE:-2026-10-02}" \
    HTTP_ADDR="$http_addr" MOCK_STREAM_PACING_MS=0 \
    "$gateway" >"$log_path" 2>&1 &
  printf '%s' "$!"
}

admin_pid=$(start_gateway demo-admin-a "$admin_addr" "$admin_log")
member_pid=$(start_gateway demo-agent-east "$member_addr" "$member_log")

for address in "$admin_addr" "$member_addr"; do
  ready=0
  for _ in $(seq 1 60); do
    if curl -fsS "http://$address/healthz" >/dev/null 2>&1; then
      ready=1
      break
    fi
    sleep 1
  done
  if [[ "$ready" != 1 ]]; then
    printf 'NOT_RUN synthetic gateway health did not become ready (%s)\n' "$address" >&2
    exit 1
  fi
done

ADMIN_HTTP_BASE="http://$admin_addr" \
ADMIN_MEMBER_HTTP_BASE="http://$member_addr" \
python3 tests/admin_control_plane_smoke.py >"$admin_report"

after_versions=$(PGPASSWORD="$PGPASSWORD" psql -X -Atqc \
  "SELECT count(*) FROM document_version WHERE quality_json ? 'artifact_bundle'")
after_chunks=$(PGPASSWORD="$PGPASSWORD" psql -X -Atqc \
  "SELECT count(*) FROM chunk WHERE source_object_key LIKE '%/raw/%' AND parsed_object_key LIKE '%/parsed/%'")
bound_versions=$(PGPASSWORD="$PGPASSWORD" psql -X -Atqc \
  "SELECT count(*) FROM document_version WHERE quality_json ? 'artifact_bundle' AND object_key LIKE '%/raw/%' AND parsed_object_key LIKE '%/parsed/%' AND quality_json->'artifact_bundle'->>'real_service_acceptance'='false'")
bound_chunks=$(PGPASSWORD="$PGPASSWORD" psql -X -Atqc \
  "SELECT count(*) FROM chunk WHERE source_object_key LIKE '%/raw/%' AND parsed_object_key LIKE '%/parsed/%'")
fenced_versions=$(PGPASSWORD="$PGPASSWORD" psql -X -Atqc \
  "SELECT count(*) FROM document_version WHERE quality_json ? 'artifact_bundle' AND fencing_epoch > 0")

new_versions=$((after_versions - before_versions))
new_chunks=$((after_chunks - before_chunks))
if (( new_versions < 1 || new_chunks < 1 || bound_versions < 1 || bound_chunks < 1 || fenced_versions < 1 )); then
  printf 'FAIL artifact binding database verification failed\n' >&2
  exit 1
fi

jq -n \
  --argjson admin "$(cat "$admin_report")" \
  --arg before_versions "$before_versions" \
  --arg after_versions "$after_versions" \
  --arg new_versions "$new_versions" \
  --arg before_chunks "$before_chunks" \
  --arg after_chunks "$after_chunks" \
  --arg new_chunks "$new_chunks" \
  --arg bound_versions "$bound_versions" \
  --arg bound_chunks "$bound_chunks" \
  --arg fenced_versions "$fenced_versions" \
  ' {
      "status": "PASS",
      "profile": "synthetic_import_mock",
      "real_service_acceptance": false,
      "admin_smoke": $admin,
      "database_binding": {
        "artifact_versions_before": ($before_versions|tonumber),
        "artifact_versions_after": ($after_versions|tonumber),
        "new_artifact_versions": ($new_versions|tonumber),
        "artifact_chunks_before": ($before_chunks|tonumber),
        "artifact_chunks_after": ($after_chunks|tonumber),
        "new_artifact_chunks": ($new_chunks|tonumber),
        "bound_versions": ($bound_versions|tonumber),
        "bound_chunks": ($bound_chunks|tonumber),
        "fenced_versions": ($fenced_versions|tonumber)
      },
      "raw_content_saved": false,
      "online_provider_called": false,
      "public_crawler_called": false
    }' >"$report_path"
chmod 600 "$report_path"
printf 'PASS artifact bundle worker binding; report=%s real_service_acceptance=false\n' "$report_path"
