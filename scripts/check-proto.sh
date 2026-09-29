#!/usr/bin/env bash
set -euo pipefail
repo=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
cd "$repo"
buf lint
buf build -o /dev/null
base=${PROTO_BREAKING_BASE:-.git#branch=main}
buf breaking --against "$base"
