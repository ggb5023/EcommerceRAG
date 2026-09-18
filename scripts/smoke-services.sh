#!/usr/bin/env bash
set -euo pipefail
repo=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
cd "$repo"
install -d .local/smoke
(cd go; go build -p 2 -o ../.local/smoke/gateway ./cmd/server)
HTTP_ADDR=127.0.0.1:18080 .local/smoke/gateway > .local/smoke/go.log 2>&1 &
go_pid=$!
(cd python; exec env GRPC_ADDR=127.0.0.1:15051 .venv/bin/python -m app.server) > .local/smoke/python.log 2>&1 &
py_pid=$!
(cd web; exec ./node_modules/.bin/vite --host 127.0.0.1 --port 15173 --strictPort) > .local/smoke/web.log 2>&1 &
web_pid=$!
cleanup() {
  kill "$go_pid" "$py_pid" "$web_pid" 2>/dev/null || true
  wait "$go_pid" "$py_pid" "$web_pid" 2>/dev/null || true
}
trap cleanup EXIT
for _ in $(seq 1 30); do
  if curl -fsS http://127.0.0.1:18080/healthz >/dev/null 2>&1 &&
     grpcurl -plaintext -d '{}' 127.0.0.1:15051 grpc.health.v1.Health/Check >/dev/null 2>&1 &&
     curl -fsS http://127.0.0.1:15173 >/dev/null 2>&1; then
    echo 'PASS gateway health, gRPC health and Vue dev preview'
    exit 0
  fi
  sleep 1
done
echo 'FAIL health probes; see .local/smoke logs'
exit 1
