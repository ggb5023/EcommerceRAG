#!/usr/bin/env bash
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "root required"; exit 1; }
. /etc/os-release
[[ "$ID" == ubuntu && "$VERSION_ID" == 24.04 ]] || { echo "Ubuntu 24.04 required"; exit 1; }
if ! command -v docker >/dev/null; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y -qq docker.io=29.1.3-0ubuntu3~24.04.2 docker-compose-v2=2.40.3+ds1-0ubuntu1~24.04.1 docker-buildx=0.30.1-0ubuntu1~24.04.1 ca-certificates curl jq unzip
fi
systemctl enable --now docker
docker --version
docker compose version
[[ "$(docker version --format '{{.Server.Version}}')" == 29.1.3 ]] || { echo 'Unexpected Docker version; review compatibility before proceeding'; exit 1; }
