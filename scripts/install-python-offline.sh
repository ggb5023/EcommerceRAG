#!/usr/bin/env bash
set -euo pipefail
repo=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
wheelhouse=${1:?Usage: install-python-offline.sh /absolute/path/to/verified/wheels}
wheelhouse=$(realpath "$wheelhouse")
[[ -d "$wheelhouse" ]] || exit 1
cd "$repo/python"
[[ -x .venv/bin/python ]] || uv venv --python 3.12.14 .venv
install -d "$repo/.local"
uv export --frozen --format requirements-txt --no-emit-project -o "$repo/.local/requirements.lock.txt" >/dev/null
uv pip sync --python .venv/bin/python --require-hashes --no-index --find-links "$wheelhouse" "$repo/.local/requirements.lock.txt"
echo 'PASS offline installation using uv.lock package hashes'
