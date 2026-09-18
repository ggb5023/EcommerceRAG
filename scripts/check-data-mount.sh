#!/usr/bin/env bash
set -euo pipefail
repo=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
expected=$(python3 "$repo/scripts/infra_config.py" ECR_DATA_UUID)
[[ "$(findmnt -n -o UUID --target /data)" == "$expected" ]] ||
  { echo "Refusing startup: /data UUID differs or mount is absent"; exit 1; }
mountpoint -q /data
