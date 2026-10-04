#!/usr/bin/env bash
set -euo pipefail
repo=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
cd "$repo"
python3 scripts/privacy_guard.py
python3 -m unittest discover -s tests -p 'test_privacy*.py'
python3 -m unittest discover -s tests -p 'test_worker_process_harness.py'
python3 scripts/check-docs.py
python3 scripts/check-doc-baseline.py
while IFS= read -r -d '' script; do bash -n "$script"; done < <(find scripts docker -name '*.sh' -print0)
shellcheck -S warning scripts/*.sh docker/db/*.sh docker/db/init/*.sh
(cd go; go test -p 2 ./...; go vet ./...)
(cd python; uv sync --frozen; uv run ruff check app; uv run python -c 'from app.providers.contracts import EmbeddingResult; from app import server')
buf lint
buf build -o /dev/null
buf breaking --against .git#branch=main
(cd web; npm ci --no-audit --no-fund; npm run build)
(cd admin-web; npm ci --no-audit --no-fund; npm test; npm run build)
echo "PASS foundation checks (business and real API tests not included)"
