#!/usr/bin/env bash
set -euo pipefail
repo=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
cd "$repo"
git config --local core.hooksPath .githooks
git config --local user.name 'EcommerceRAG Maintainers'
git config --local user.email 'maintainers@example.invalid'
chmod +x .githooks/pre-commit .githooks/pre-push
echo 'PASS local Git hooks and public commit identity configured'
