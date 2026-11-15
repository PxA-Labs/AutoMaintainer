#!/usr/bin/env bash
# End-to-end smoke test of the docker compose topology (#396).
#
# Builds the image, starts api + worker + beat + redis from docker-compose.yml
# plus a test-only override (scripts/compose-smoke/compose.smoke.yml) that adds
# an in-memory fake of Supabase's REST API and turns on the dev auth bypass,
# then checks, in order:
#
#   1. GET /healthz answers 200 (the API is up).
#   2. The Celery worker answers a ping (worker <-> Redis).
#   3. api and worker see the same workspace volume.
#   4. POST /start is accepted. /start returns 503 when no worker consumes
#      agent_runs, so acceptance proves api -> Redis -> worker.
#   5. The run reaches a terminal state, and the worker processed it.
#
# No real credentials are used. The expected terminal state is "failed" with
# "GitHub credentials unavailable": the worker picks the run up, marks it
# running, and the agent stops at its first GitHub step because no GitHub App
# is configured. That exercises queue -> worker -> database end to end without
# calling GitHub or the LLM provider.
#
# Logs of every service are printed and the stack (including volumes) is torn
# down on exit, whatever the outcome.
#
# Environment:
#   COMPOSE_SMOKE_SKIP_BUILD=1    use an existing automaintainer:local image
#   COMPOSE_SMOKE_TIMEOUT         seconds to wait for each step (default 180)
#   COMPOSE_PROJECT_NAME          default automaintainer-smoke, so a running
#                                 dev stack's volumes are never touched
set -euo pipefail

cd "$(dirname "$0")/.."

export COMPOSE_PROJECT_NAME="${COMPOSE_PROJECT_NAME:-automaintainer-smoke}"
TIMEOUT="${COMPOSE_SMOKE_TIMEOUT:-180}"
# docker-compose.yml publishes the API on host port 8000, so a dev stack that
# is already running on that port must be stopped first.
BASE_URL="http://127.0.0.1:8000"
REPO="smoke-test/fixture-repo" # seeded in compose.smoke.yml
EXPECTED_ERROR="GitHub credentials unavailable"

compose() {
  docker compose -f docker-compose.yml -f scripts/compose-smoke/compose.smoke.yml "$@"
}

log() { printf '\n==> %s\n' "$*"; }
fail() {
  printf '\nSMOKE TEST FAILED: %s\n' "$*" >&2
  exit 1
}

cleanup() {
  local rc=$?
  set +e
  log "docker compose ps"
  compose ps -a
  log "docker compose logs"
  compose logs --no-color --timestamps
  log "tearing down"
  compose down --volumes --remove-orphans --timeout 20
  rm -f "${start_body:-}" "${status_body:-}"
  if [ "$rc" -eq 0 ]; then
    log "SMOKE TEST PASSED"
  fi
  exit "$rc"
}
trap cleanup EXIT
# Ctrl-C / cancellation: exit so the EXIT trap still tears the stack down.
trap 'exit 130' INT TERM

# Retry "$@" every 3 s until it succeeds or TIMEOUT seconds have passed.
wait_for() {
  local what=$1
  shift
  local deadline=$((SECONDS + TIMEOUT))
  until "$@"; do
    if [ "$SECONDS" -ge "$deadline" ]; then
      fail "timed out after ${TIMEOUT}s waiting for ${what}"
    fi
    sleep 3
  done
}

# JSON field of stdin, read with the host's python3 (present on CI runners).
json_field() {
  python3 -c 'import json, sys; v = json.load(sys.stdin).get(sys.argv[1]); print("" if v is None else v)' "$1"
}

command -v docker >/dev/null || fail "docker is not installed"
docker compose version >/dev/null || fail "docker compose v2 is required"
command -v curl >/dev/null || fail "curl is required"
command -v python3 >/dev/null || fail "python3 is required"

log "validating compose configuration"
compose config --quiet

if [ "${COMPOSE_SMOKE_SKIP_BUILD:-0}" = "1" ]; then
  log "using the existing automaintainer:local image"
  docker image inspect automaintainer:local >/dev/null \
    || fail "COMPOSE_SMOKE_SKIP_BUILD=1 but automaintainer:local does not exist"
else
  log "building the image"
  compose build api
fi

log "starting the stack"
compose up -d --no-build

log "1/5 waiting for GET /healthz"
wait_for "/healthz" curl -fsS -o /dev/null "${BASE_URL}/healthz"

log "2/5 waiting for the Celery worker to answer a ping"
worker_ping() {
  compose exec -T worker sh -c \
    'celery -A celery_app inspect ping -d "celery@$HOSTNAME" --timeout 5' >/dev/null 2>&1
}
wait_for "a Celery worker ping" worker_ping

log "3/5 checking that api and worker share the workspace volume"
marker="smoke-$$-$RANDOM"
compose exec -T worker sh -c "echo '$marker' > \"\$AUTOMAINTAINER_WORKSPACE_DIR/.compose-smoke\""
seen=$(compose exec -T api sh -c 'cat "$AUTOMAINTAINER_WORKSPACE_DIR/.compose-smoke"' | tr -d '\r')
[ "$seen" = "$marker" ] || fail "api did not see the worker's workspace file (got '$seen')"

log "4/5 POST /start (503 means the API found no agent worker)"
start_body=$(mktemp)
start_run() {
  local code
  code=$(curl -sS -o "$start_body" -w '%{http_code}' -X POST "${BASE_URL}/start" \
    -H 'Content-Type: application/json' \
    -d "{\"repo_name\": \"${REPO}\"}") || return 1
  echo "POST /start -> HTTP ${code}: $(cat "$start_body")"
  case "$code" in
    200) return 0 ;;
    503) return 1 ;; # worker probe not answered yet; /start's cache is 5 s
    *) fail "POST /start returned HTTP ${code}" ;;
  esac
}
wait_for "POST /start to find an agent worker" start_run
run_id=$(json_field run_id <"$start_body")
[ -n "$run_id" ] || fail "POST /start returned no run_id"
echo "run_id=${run_id}"

log "5/5 waiting for run ${run_id} to reach a terminal state"
status_body=$(mktemp)
run_is_terminal() {
  curl -fsS -o "$status_body" "${BASE_URL}/status/${run_id}" || return 1
  local status
  status=$(json_field status <"$status_body")
  echo "status=${status}"
  case "$status" in
    completed | failed | cancelled) return 0 ;;
    *) return 1 ;;
  esac
}
wait_for "run ${run_id} to finish" run_is_terminal

echo "final run row: $(cat "$status_body")"
status=$(json_field status <"$status_body")
error_message=$(json_field error_message <"$status_body")
started_at=$(json_field started_at <"$status_body")
celery_task_id=$(json_field celery_task_id <"$status_body")

# started_at is written by the worker when it picks the run up, and
# celery_task_id by the API after enqueueing: both prove the run went
# through the queue rather than failing inside the API.
[ -n "$celery_task_id" ] || fail "run has no celery_task_id: it was never enqueued"
[ -n "$started_at" ] || fail "run has no started_at: no worker picked it up"
[ "$status" = "failed" ] || fail "expected status 'failed' (no credentials), got '${status}'"
case "$error_message" in
  *"$EXPECTED_ERROR"*) ;;
  *) fail "expected error containing '${EXPECTED_ERROR}', got '${error_message}'" ;;
esac
echo "terminal state reached: ${status} (${error_message})"
