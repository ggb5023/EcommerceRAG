#!/usr/bin/env bash
set -euo pipefail
expected=00000000-0000-0000-0000-000000000000
[[ "$(findmnt -n -o UUID --target /data)" == "$expected" ]] ||
  { echo "Refusing startup: /data UUID differs or mount is absent"; exit 1; }
mountpoint -q /data
