#!/usr/bin/env bash
set -euo pipefail

# Controlled runtime harness for the Go gateway process that hosts the
# synthetic ingestion worker. It never starts a service or discovers secrets.
# The operator supplies a loopback endpoint, an exact gateway PID, a PID file
# maintained by an approved restart script, and a job that is being processed.

base=${ADMIN_HTTP_BASE:-http://127.0.0.1:8082}
pid_file=${INGEST_WORKER_PID_FILE:-}
restart_script=${INGEST_WORKER_RESTART_SCRIPT:-}
job_id=${INGEST_WORKER_JOB_ID:-}
shop_id=${INGEST_WORKER_SHOP_ID:-demo-shop-east}
old_pid=${INGEST_WORKER_PID:-}
lease_wait=${INGEST_WORKER_LEASE_WAIT_SECONDS:-50}
restart_timeout=${INGEST_WORKER_RESTART_TIMEOUT_SECONDS:-30}
settle_timeout=${INGEST_WORKER_SETTLE_TIMEOUT_SECONDS:-90}

not_run() {
  echo "NOT_RUN/CONFIG_BLOCKED $*"
  exit 3
}

fail() {
  echo "FAIL $*"
  exit 1
}

case "$base" in
  http://127.0.0.1:*|http://localhost:*|http://\[::1\]:*) ;;
  *) not_run "ADMIN_HTTP_BASE must be a loopback URL" ;;
esac

[[ -n "$pid_file" ]] || not_run "INGEST_WORKER_PID_FILE is not set"
[[ -n "$restart_script" ]] || not_run "INGEST_WORKER_RESTART_SCRIPT is not set"
[[ -n "$job_id" ]] || not_run "INGEST_WORKER_JOB_ID is not set"
[[ -f "$pid_file" ]] || not_run "worker PID file does not exist"
[[ -x "$restart_script" ]] || not_run "restart script is not executable"

read_pid() {
  local value
  value=$(cat -- "$pid_file")
  [[ "$value" =~ ^[0-9]+$ ]] || fail "worker PID file does not contain a numeric PID"
  printf '%s' "$value"
}

if [[ -z "$old_pid" ]]; then
  old_pid=$(read_pid)
fi
[[ "$old_pid" =~ ^[0-9]+$ ]] || fail "INGEST_WORKER_PID is not numeric"
kill -0 "$old_pid" 2>/dev/null || not_run "worker process is not running"

request_json() {
  local path=$1
  curl --fail-with-body --silent --show-error --max-time 10 "$base$path"
}

json_value() {
  local expression=$1
  python3 -c 'import json,sys
payload=json.load(sys.stdin)
value=payload
for part in sys.argv[1].split("."):
    if isinstance(value, dict):
        value=value.get(part)
    else:
        value=None
        break
if value is None:
    raise SystemExit(2)
print(value)' "$expression"
}

encoded_shop=$(python3 -c 'import urllib.parse,sys; print(urllib.parse.quote(sys.argv[1], safe=""))' "$shop_id")
job_path="/admin/v1/merchant/ingestion/${job_id}?shop_id=${encoded_shop}"
job_json=$(request_json "$job_path") || fail "could not read ingestion job"
status=$(printf '%s' "$job_json" | json_value status) || fail "job response has no status"
if [[ "$status" != "running" ]]; then
  not_run "job must be running before the crash rehearsal (current=$status)"
fi
before_epoch=$(printf '%s' "$job_json" | json_value fencing_epoch) || fail "job response has no fencing_epoch"

# This is the destructive part of the rehearsal. It is limited to the exact
# PID supplied by the operator and is never inferred from a service name.
kill -KILL "$old_pid" 2>/dev/null || fail "could not kill the supplied worker gateway PID"
deadline=$((SECONDS + 10))
while kill -0 "$old_pid" 2>/dev/null; do
  (( SECONDS < deadline )) || fail "killed process did not exit"
  sleep 0.2
done
echo "killed_pid=$old_pid"

# The current gateway lease is 45 seconds. Waiting longer is required before
# a new process can claim the same job.
if (( lease_wait < 45 )); then
  not_run "INGEST_WORKER_LEASE_WAIT_SECONDS must be at least 45"
fi
sleep "$lease_wait"

# The restart script is an explicit operator-owned action. It must obtain its
# environment from a protected file or service manager and update pid_file.
"$restart_script" || fail "approved restart script failed"

deadline=$((SECONDS + restart_timeout))
new_pid=""
while (( SECONDS < deadline )); do
  if [[ -f "$pid_file" ]]; then
    candidate=$(read_pid)
    if [[ "$candidate" != "$old_pid" ]] && kill -0 "$candidate" 2>/dev/null; then
      new_pid=$candidate
      break
    fi
  fi
  sleep 0.5
done
[[ -n "$new_pid" ]] || fail "restarted gateway PID was not observed"
echo "restarted_pid=$new_pid"

deadline=$((SECONDS + settle_timeout))
while (( SECONDS < deadline )); do
  job_json=$(request_json "$job_path") || {
    sleep 0.5
    continue
  }
  status=$(printf '%s' "$job_json" | json_value status) || fail "job response has no status after restart"
  after_epoch=$(printf '%s' "$job_json" | json_value fencing_epoch) || fail "job response has no fencing_epoch after restart"
  if [[ "$status" == "awaiting_review" ]]; then
    if (( after_epoch <= before_epoch )); then
      fail "restarted worker completed without advancing fencing_epoch"
    fi
    echo "PASS worker process kill/restart lease takeover; before_epoch=$before_epoch after_epoch=$after_epoch"
    echo "real_service_acceptance=false"
    exit 0
  fi
  if [[ "$status" == "failed" || "$status" == "cancelled" ]]; then
    fail "restarted worker did not complete the job (status=$status)"
  fi
  sleep 0.5
done

fail "job did not reach awaiting_review after worker restart"
