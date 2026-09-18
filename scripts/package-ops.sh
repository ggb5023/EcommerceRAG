#!/usr/bin/env bash
set -euo pipefail
repo=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
cd "$repo"
[[ -z "$(git status --porcelain)" ]] || { echo 'Commit reviewed changes before packaging'; exit 1; }
revision=$(git rev-parse HEAD)
install -d .local/releases
archive=".local/releases/db-ops-$revision.tar.gz"
git archive --format=tar.gz -o "$archive" HEAD docker/db sql tests scripts/backup-db.sh scripts/bootstrap-db.sh scripts/check-data-mount.sh scripts/create-db-secrets.py scripts/db-firewall.sh scripts/infra_config.py scripts/install-docker.sh scripts/verify-db.sh scripts/verify-redis-restart.sh
sha256sum "$archive"
echo "Revision: $revision"
echo 'Transfer over the existing SSH connection; extract into DB ops directory, record DEPLOYED_REVISION, and re-run checks.'
