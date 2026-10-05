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
before_version_ids="$runtime_root/before-version-ids"
PGPASSWORD="$PGPASSWORD" psql -X -Atqc \
  "SELECT quality_json->'artifact_bundle'->>'document_version_id' FROM document_version WHERE quality_json ? 'artifact_bundle'" \
  >"$before_version_ids"

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

artifact_rows="$runtime_root/artifact-rows.tsv"
PGPASSWORD="$PGPASSWORD" psql -X -At -F $'\t' -c \
  "SELECT quality_json->'artifact_bundle'->>'document_version_id',
          quality_json->'artifact_bundle'->>'manifest_object_key',
          quality_json->'artifact_bundle'->>'manifest_sha256',
          quality_json->'artifact_bundle'->>'artifact_set_sha256',
          quality_json->'artifact_bundle'->'artifacts'
     FROM document_version
    WHERE quality_json ? 'artifact_bundle'" >"$artifact_rows"

object_verification="$runtime_root/object-verification.json"
python3 - "$bundle_root" "$before_version_ids" "$artifact_rows" "$object_verification" <<'PY'
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path, PurePosixPath


bundle_root, before_path, rows_path, output_path = map(Path, sys.argv[1:])
before = {line.strip() for line in before_path.read_text().splitlines() if line.strip()}
rows = []
for line in rows_path.read_text().splitlines():
    if not line.strip():
        continue
    fields = line.split("\t", 4)
    if len(fields) != 5:
        raise SystemExit("artifact database row is malformed")
    rows.append(fields)

new_rows = [row for row in rows if row[0] not in before]
if not new_rows:
    raise SystemExit("no newly persisted artifact bundle rows were found")


def target_for(key: str) -> Path:
    path = PurePosixPath(key)
    if (not key or "\\" in key or path.is_absolute()
            or any(part in {"", ".", ".."} for part in path.parts)):
        raise SystemExit("artifact object key is unsafe")
    target = bundle_root.joinpath(*path.parts)
    try:
        target.resolve().relative_to(bundle_root.resolve())
    except ValueError as exc:
        raise SystemExit("artifact object key escapes bundle root") from exc
    return target


def digest(path: Path) -> tuple[str, int]:
    data = path.read_bytes()
    return hashlib.sha256(data).hexdigest(), len(data)


verified_objects = 0
verified_manifests = 0
verified_records = 0
for version_id, manifest_key, manifest_sha, artifact_set_sha, artifacts_text in new_rows:
    try:
        artifacts = json.loads(artifacts_text)
    except json.JSONDecodeError as exc:
        raise SystemExit("artifact records are not valid JSON") from exc
    if not isinstance(artifacts, list) or len(artifacts) != 4:
        raise SystemExit("artifact bundle must contain exactly four artifact records")

    manifest_path = target_for(manifest_key)
    manifest_digest, _ = digest(manifest_path)
    if manifest_digest != manifest_sha:
        raise SystemExit("artifact manifest SHA-256 does not match the database")
    manifest = json.loads(manifest_path.read_text())
    if (manifest.get("document_version_id") != version_id
            or manifest.get("real_service_acceptance") is not False
            or manifest.get("artifact_set_sha256") != artifact_set_sha
            or manifest.get("artifacts") != artifacts):
        raise SystemExit("artifact manifest does not match database metadata")
    canonical = json.dumps(
        {"artifacts": artifacts}, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"),
    ).encode()
    actual_set_sha = hashlib.sha256(canonical).hexdigest()
    if actual_set_sha != artifact_set_sha:
        raise SystemExit("artifact set SHA-256 does not match the database")
    verified_objects += 1
    verified_manifests += 1

    for record in artifacts:
        if not isinstance(record, dict):
            raise SystemExit("artifact record is not an object")
        artifact_type = record.get("artifact_type")
        object_key = record.get("object_key")
        expected_sha = record.get("sha256")
        expected_size = record.get("size_bytes")
        parts = PurePosixPath(object_key).parts if isinstance(object_key, str) else ()
        if (len(parts) != 5 or parts[2] != version_id
                or parts[3] != artifact_type or parts[4] != expected_sha):
            raise SystemExit("artifact object key is not bound to its record")
        path = target_for(object_key)
        if not path.is_file():
            raise SystemExit("artifact object is missing from the bundle root")
        actual_sha, actual_size = digest(path)
        if actual_sha != expected_sha or actual_size != expected_size:
            raise SystemExit("artifact object hash or size does not match the database")
        verified_objects += 1
        verified_records += 1

Path(output_path).write_text(json.dumps({
    "document_versions_verified": len(new_rows),
    "artifact_records_verified": verified_records,
    "objects_verified": verified_objects,
    "manifest_objects_verified": verified_manifests,
}, sort_keys=True) + "\n")
PY

jq -n \
  --argjson admin "$(cat "$admin_report")" \
  --argjson object_verification "$(cat "$object_verification")" \
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
        "fenced_versions": ($fenced_versions|tonumber),
        "object_verification": $object_verification
      },
      "raw_content_saved": false,
      "online_provider_called": false,
      "public_crawler_called": false
    }' >"$report_path"
chmod 600 "$report_path"
printf 'PASS artifact bundle worker binding; report=%s real_service_acceptance=false\n' "$report_path"
