#!/bin/bash
# Self-hosted Redis for redis_mode = "vm": the Celery broker, result backend,
# locks and rate limits. noeviction and appendonly as in docker-compose.yml.
# The VPC firewall and this host firewall admit only the subnet (Cloud Run's
# Direct VPC egress and this VM).
#
# Redis AUTH (#532): when REDIS_URL_SECRET names a Secret Manager secret, its
# latest version is the broker URL redis://:<password>@<host>:<port>/<db>
# that the API and the worker use. The password is read from it with the VM
# service account and handed to redis-server as `requirepass` on stdin
# (`redis-server -` reads its configuration from stdin), so it is never on a
# command line, in `docker inspect` or on disk. Nothing here prints it. Any
# failure exits 1 and systemd retries; until the owner adds the version,
# Redis does not start.
set -euo pipefail

# shellcheck source=/dev/null
source "${AUTOMAINTAINER_WORKER_ENV:-/etc/automaintainer/worker.env}"

fail() {
  echo "run-redis: $*" >&2
  exit 1
}

# The password from the secret, on stdout. Only the standard tools of
# Container-Optimized OS (curl, sed, tr, base64): no jq, no Python.
redis_password() {
  local token_json token body data url rest userinfo password
  token_json="$(curl -fsS -H 'Metadata-Flavor: Google' \
    'http://169.254.169.254/computeMetadata/v1/instance/service-accounts/default/token')" ||
    fail "REDIS_URL_SECRET: cannot get an access token from the metadata server"
  token="$(printf '%s' "$token_json" | tr -d ' \n' |
    sed -n 's/.*"access_token":"\([^"]*\)".*/\1/p')"
  [ -n "$token" ] || fail "REDIS_URL_SECRET: no access token in the metadata answer"
  # The header goes through stdin (-H @-), not the command line.
  body="$(printf 'Authorization: Bearer %s\n' "$token" |
    curl -fsS -H @- \
      "https://secretmanager.googleapis.com/v1/${REDIS_URL_SECRET}/versions/latest:access")" ||
    fail "REDIS_URL_SECRET: cannot read the latest version of the secret"
  data="$(printf '%s' "$body" | tr -d ' \n' |
    sed -n 's/.*"data":"\([^"]*\)".*/\1/p')"
  url="$(printf '%s' "$data" | base64 -d 2>/dev/null)" ||
    fail "REDIS_URL_SECRET: the secret payload is not base64"
  rest="${url#redis*://}"
  [ "$rest" != "$url" ] || fail "REDIS_URL_SECRET: the secret is not a redis:// URL"
  userinfo="${rest%%@*}"
  [ "$userinfo" != "$rest" ] || fail "REDIS_URL_SECRET: the URL has no password"
  password="${userinfo#*:}"
  # URL-safe characters only, so the URL needs no escaping, and long enough
  # to resist guessing (openssl rand -hex 32 gives 64).
  [[ "$password" =~ ^[A-Za-z0-9._~-]{32,}$ ]] ||
    fail "REDIS_URL_SECRET: the password must be at least 32 characters from A-Z a-z 0-9 . _ ~ -"
  printf '%s' "$password"
}

redis_config=""
if [ -n "${REDIS_URL_SECRET:-}" ]; then
  password="$(redis_password)"
  redis_config="requirepass ${password}"
  unset password
fi

# Container-Optimized OS drops inbound traffic except SSH by default.
rule=(INPUT -p tcp --dport 6379 -s "$REDIS_ALLOW_CIDR" -j ACCEPT)
iptables -w -C "${rule[@]}" 2>/dev/null || iptables -w -A "${rule[@]}"

docker rm -f automaintainer-redis >/dev/null 2>&1 || true
# printf is a shell builtin: the configuration does not appear in `ps`.
exec docker run -i --rm --name automaintainer-redis --network host \
  -v automaintainer-redis:/data \
  "$REDIS_IMAGE" \
  redis-server - --port 6379 --protected-mode no \
  --appendonly yes --maxmemory "$REDIS_MAXMEMORY" --maxmemory-policy noeviction \
  < <(if [ -n "$redis_config" ]; then printf '%s\n' "$redis_config"; fi)
