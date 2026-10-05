#!/usr/bin/env bash
set -euo pipefail

repo=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
cd "$repo"

env_file=${SYNTHETIC_REVIEW_ENV:-/etc/ecommerce-rag/synthetic-review.env}
report_dir=${P5_REPORT_DIR:-.local/smoke/p5-current}
if [[ "$report_dir" != /* ]]; then
  report_dir="$repo/$report_dir"
fi
if [[ ! -r "$env_file" ]]; then
  printf 'BLOCKED synthetic review env is not readable: %s\n' "$env_file" >&2
  exit 2
fi

set -a
source "$env_file"
set +a
: "${GRPC_ADDR:?GRPC_ADDR is required}"
: "${RAG_GRPC_ADDR:?RAG_GRPC_ADDR is required}"
: "${HTTP_ADDR:?HTTP_ADDR is required}"
: "${SYNTHETIC_MANIFEST:?SYNTHETIC_MANIFEST is required}"

mkdir -p "$report_dir"
(cd go && go build -p 2 -o "$report_dir/gateway" ./cmd/server)

py_pid=
go_pid=
cleanup() {
  for pid in "$go_pid" "$py_pid"; do
    if [[ -n "$pid" ]]; then
      kill "$pid" 2>/dev/null || true
    fi
  done
  for pid in "$go_pid" "$py_pid"; do
    if [[ -n "$pid" ]]; then
      wait "$pid" 2>/dev/null || true
    fi
  done
}
trap cleanup EXIT

# The gateway restores its synthetic index during startup, so gRPC must be
# serving before the gateway process is started.
(cd python && exec env \
  GRPC_ADDR="$GRPC_ADDR" \
  RAG_PROFILE="$RAG_PROFILE" \
  SYNTHETIC_MANIFEST="$SYNTHETIC_MANIFEST" \
  MOCK_STREAM_PACING_MS="${MOCK_STREAM_PACING_MS:-0}" \
  .venv/bin/python -m app.server) > "$report_dir/python.log" 2>&1 &
py_pid=$!

grpc_ready=0
for _ in $(seq 1 60); do
  if grpcurl -plaintext -d '{}' "$GRPC_ADDR" grpc.health.v1.Health/Check >/dev/null 2>&1; then
    grpc_ready=1
    break
  fi
  sleep 1
done
if [[ "$grpc_ready" != 1 ]]; then
  printf 'NOT_RUN synthetic gRPC health did not become ready\n' >&2
  tail -80 "$report_dir/python.log" >&2 || true
  exit 1
fi

(env \
  APP_ENV="$APP_ENV" \
  PGHOST="$PGHOST" \
  PGPORT="$PGPORT" \
  PGDATABASE="$PGDATABASE" \
  PGUSER="$PGUSER" \
  PGPASSWORD="$PGPASSWORD" \
  RAG_GRPC_ADDR="$RAG_GRPC_ADDR" \
  RAG_PROFILE="$RAG_PROFILE" \
  SYNTHETIC_MANIFEST="$SYNTHETIC_MANIFEST" \
  SYNTHETIC_USER_ID="$SYNTHETIC_USER_ID" \
  SYNTHETIC_SHOP_ID="$SYNTHETIC_SHOP_ID" \
  SYNTHETIC_BUSINESS_DATE="$SYNTHETIC_BUSINESS_DATE" \
  MOCK_STREAM_PACING_MS="${MOCK_STREAM_PACING_MS:-0}" \
  HTTP_ADDR="$HTTP_ADDR" \
  "$report_dir/gateway") > "$report_dir/go.log" 2>&1 &
go_pid=$!

gateway_ready=0
for _ in $(seq 1 60); do
  if curl -fsS "http://$HTTP_ADDR/healthz" >/dev/null 2>&1; then
    gateway_ready=1
    break
  fi
  sleep 1
done
if [[ "$gateway_ready" != 1 ]]; then
  printf 'NOT_RUN synthetic gateway health did not become ready\n' >&2
  tail -80 "$report_dir/python.log" >&2 || true
  tail -80 "$report_dir/go.log" >&2 || true
  exit 1
fi

SYNTHETIC_HTTP_BASE="http://$HTTP_ADDR" python3 tests/synthetic_workspace_smoke.py \
  > "$report_dir/report.json"
python3 - "$report_dir/report.json" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    report = json.load(handle)
print(json.dumps({
    "profile": report.get("profile"),
    "real_service_acceptance": report.get("real_service_acceptance"),
    "checks": report.get("checks"),
}, ensure_ascii=False, sort_keys=True))
PY
