#!/usr/bin/env bash
set -euo pipefail
umask 077
repo=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
bash "$repo/scripts/check-data-mount.sh"
install -d -m 700 /data/backups
exec 9>/run/lock/ecommerce-rag-backup.lock
flock -n 9 || { echo "Backup already running"; exit 1; }
available=$(df --output=avail -k /data | tail -1)
(( available > 2097152 )) || { echo "Less than 2 GiB free; backup deferred"; exit 1; }
name="/data/backups/rag-$(date -u +%Y%m%dT%H%M%SZ).dump"
docker exec rag-postgres pg_dump -U postgres -d rag -Fc > "$name.partial"
docker exec -i rag-postgres pg_restore --list < "$name.partial" >/dev/null
mv -- "$name.partial" "$name"
sha256sum "$name" > "$name.sha256"
find /data/backups -maxdepth 1 -type f \( -name 'rag-????????T??????Z.dump' -o -name 'rag-????????T??????Z.dump.sha256' \) -mtime +6 -delete
echo "PASS backup $name"
df -h /data
