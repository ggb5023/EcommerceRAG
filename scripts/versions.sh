#!/usr/bin/env bash
set -euo pipefail
go version
node --version
npm --version
uv --version
uv python find 3.12.14
buf --version
grpcurl --version
docker --version
docker compose version
codex --version
psql --version
redis-cli --version
