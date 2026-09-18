#!/usr/bin/env bash
set -euo pipefail
install -d -m 700 /root/.ssh
for host in gitee github; do
  key="/root/.ssh/ecommerce-rag-${host}"
  if [[ ! -e "$key" ]]; then
    ssh-keygen -q -t ed25519 -N '' -C "EcommerceRAG-dev-${host}" -f "$key"
  fi
done
echo 'GITEE_PUBLIC_KEY'
cat /root/.ssh/ecommerce-rag-gitee.pub
echo 'GITHUB_PUBLIC_KEY'
cat /root/.ssh/ecommerce-rag-github.pub
