#!/usr/bin/env bash
set -euo pipefail
[[ $EUID -eq 0 ]] || exit 1
export DEBIAN_FRONTEND=noninteractive
repo=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
dev_ip=$(python3 "$repo/scripts/infra_config.py" ECR_DEV_PRIVATE_IP)
[[ "$repo" == /opt/ecommerce-rag ]] || { echo 'Run from Dev workspace'; exit 1; }
ip -4 -o addr show | awk '{print $4}' | cut -d/ -f1 | grep -Fxq "$dev_ip" || { echo 'Configured Dev private IP not found'; exit 1; }
bash "$repo/scripts/install-docker.sh"
apt-get install -y -qq build-essential git tmux postgresql-client-16 redis-tools protobuf-compiler python3-venv shellcheck
install -d -m 700 /etc/ecommerce-rag
install -d /opt/ecommerce-toolchains /usr/local/bin
cache="$repo/.local/toolchains"
install -d "$cache"
fetch() {
  local url=$1 dest=$2
  if [[ ! -s "$dest" ]]; then
    curl -fsSL --connect-timeout 10 --max-time 180 --retry 2 "$url" -o "$dest.partial"
    mv "$dest.partial" "$dest"
  fi
}
if [[ ! -d /opt/ecommerce-toolchains/go1.27.1 ]]; then
  echo '63d339f0da5ab53635a56f2490a7984dfe12dfcff22ad749f63edaf590168445  /tmp/go1.27.1.linux-amd64.tar.gz' | sha256sum -c -
  install -d /opt/ecommerce-toolchains/go1.27.1
  tar -xzf /tmp/go1.27.1.linux-amd64.tar.gz --strip-components=1 -C /opt/ecommerce-toolchains/go1.27.1
fi
ln -sfn /opt/ecommerce-toolchains/go1.27.1/bin/go /usr/local/bin/go
ln -sfn /opt/ecommerce-toolchains/go1.27.1/bin/gofmt /usr/local/bin/gofmt
nodever=v24.21.0
if [[ ! -d "/opt/ecommerce-toolchains/node-$nodever" ]]; then
  curl -fsSL --retry 3 "https://nodejs.org/dist/$nodever/SHASUMS256.txt" -o "$cache/node-SHASUMS256.txt"
  curl -fsSL --retry 3 "https://nodejs.org/dist/$nodever/node-$nodever-linux-x64.tar.xz" -o "$cache/node-$nodever-linux-x64.tar.xz"
  (cd "$cache"; grep " node-$nodever-linux-x64.tar.xz$" node-SHASUMS256.txt | sha256sum -c -)
  install -d "/opt/ecommerce-toolchains/node-$nodever"
  tar -xJf "$cache/node-$nodever-linux-x64.tar.xz" --strip-components=1 -C "/opt/ecommerce-toolchains/node-$nodever"
fi
for binary in node npm npx; do ln -sfn "/opt/ecommerce-toolchains/node-$nodever/bin/$binary" "/usr/local/bin/$binary"; done
if ! command -v uv >/dev/null; then
  fetch https://github.com/astral-sh/uv/releases/download/0.12.16/uv-x86_64-unknown-linux-gnu.tar.gz "$cache/uv.tar.gz"
  fetch https://github.com/astral-sh/uv/releases/download/0.12.16/uv-x86_64-unknown-linux-gnu.tar.gz.sha256 "$cache/uv.sha256"
  expected=$(awk '{print $1}' "$cache/uv.sha256")
  echo "$expected  $cache/uv.tar.gz" | sha256sum -c -
  tar -xzf "$cache/uv.tar.gz" -C "$cache"
  install -m 755 "$cache/uv-x86_64-unknown-linux-gnu/uv" /usr/local/bin/uv
  install -m 755 "$cache/uv-x86_64-unknown-linux-gnu/uvx" /usr/local/bin/uvx
fi
uv python install 3.12.14
if ! command -v buf >/dev/null; then
  fetch https://github.com/bufbuild/buf/releases/download/v1.73.0/buf-Linux-x86_64 "$cache/buf-Linux-x86_64"
  fetch https://github.com/bufbuild/buf/releases/download/v1.73.0/sha256.txt "$cache/buf-sha256.txt"
  (cd "$cache"; grep ' buf-Linux-x86_64$' buf-sha256.txt | sha256sum -c -)
  install -m 755 "$cache/buf-Linux-x86_64" /usr/local/bin/buf
fi
if ! command -v grpcurl >/dev/null; then
  fetch https://github.com/fullstorydev/grpcurl/releases/download/v1.9.4/grpcurl_1.9.4_linux_x86_64.tar.gz "$cache/grpcurl_1.9.4_linux_x86_64.tar.gz"
  fetch https://github.com/fullstorydev/grpcurl/releases/download/v1.9.4/grpcurl_1.9.4_checksums.txt "$cache/grpcurl-checksums.txt"
  (cd "$cache"; grep ' grpcurl_1.9.4_linux_x86_64.tar.gz$' grpcurl-checksums.txt | sha256sum -c -)
  tar -xzf "$cache/grpcurl_1.9.4_linux_x86_64.tar.gz" -C "$cache" grpcurl
  install -m 755 "$cache/grpcurl" /usr/local/bin/grpcurl
fi
go version
node --version
uv --version
buf --version
grpcurl --version
codex --version
[[ "$(go version)" == 'go version go1.27.1 linux/amd64' ]]
[[ "$(node --version)" == v24.21.0 ]]
[[ "$(uv --version)" == 'uv 0.12.16'* ]]
[[ "$(buf --version)" == 1.73.0 ]]
