#!/usr/bin/env bash
set -euo pipefail
key="ecr:verification:restart:$(date +%s):$$"
value="fixture-$$"
redis() { docker exec rag-redis sh -c 'export REDISCLI_AUTH=$(cat /run/secrets/redis_password); exec redis-cli --raw "$@"' sh "$@"; }
[[ "$(redis SET "$key" "$value" EX 600)" == OK ]]
redis WAITAOF 1 0 5000 >/dev/null
docker restart rag-redis >/dev/null
for _ in $(seq 1 15); do
  if [[ "$(redis PING 2>/dev/null)" == PONG ]]; then break; fi
  sleep 1
done
[[ "$(redis GET "$key")" == "$value" ]]
redis DEL "$key" >/dev/null
echo 'PASS Redis AOF restart persistence; only the generated verification key removed'
