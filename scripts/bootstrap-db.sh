#!/usr/bin/env bash
set -euo pipefail
[[ $EUID -eq 0 ]] || exit 1
repo=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
[[ "$repo" == /opt/ecommerce-rag-ops ]] || { echo "Run from DB ops workspace"; exit 1; }
python3 "$repo/scripts/infra_config.py" --check ECR_DEV_PRIVATE_IP ECR_DB_PRIVATE_IP ECR_DATA_UUID
bash "$repo/scripts/check-data-mount.sh"
bash "$repo/scripts/install-docker.sh"
install -d -m 700 /etc/ecommerce-rag /data/backups
python3 "$repo/scripts/create-db-secrets.py"
install -d /etc/systemd/system/docker.service.d
install -m 644 "$repo/docker/db/systemd/require-data.conf" /etc/systemd/system/docker.service.d/ecommerce-rag-data.conf
for unit in ecommerce-rag-db.service ecommerce-rag-backup.service ecommerce-rag-backup.timer; do
  install -m 644 "$repo/docker/db/systemd/$unit" /etc/systemd/system/
done
systemctl daemon-reload
[[ -f /etc/ecommerce-rag/db.env ]] || { echo "Prepared. Resolve official image digests into /etc/ecommerce-rag/db.env before starting."; exit 2; }
docker compose --env-file /etc/ecommerce-rag/db.env -f "$repo/docker/db/docker-compose.yml" config --quiet
systemctl enable ecommerce-rag-db.service ecommerce-rag-backup.timer
systemctl start ecommerce-rag-db.service
systemctl is-active --quiet ecommerce-rag-db.service
systemctl start ecommerce-rag-backup.timer
