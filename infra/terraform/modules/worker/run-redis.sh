#!/bin/bash
# Self-hosted Redis for redis_mode = "vm": the Celery broker, result backend,
# locks and rate limits. noeviction and appendonly as in docker-compose.yml.
# No password: the VPC firewall and this host firewall admit only the
# subnet (Cloud Run's Direct VPC egress and this VM).
set -euo pipefail

# shellcheck source=/dev/null
source /etc/automaintainer/worker.env

# Container-Optimized OS drops inbound traffic except SSH by default.
rule=(INPUT -p tcp --dport 6379 -s "$REDIS_ALLOW_CIDR" -j ACCEPT)
iptables -w -C "${rule[@]}" 2>/dev/null || iptables -w -A "${rule[@]}"

docker rm -f automaintainer-redis >/dev/null 2>&1 || true
exec docker run --rm --name automaintainer-redis --network host \
  -v automaintainer-redis:/data \
  "$REDIS_IMAGE" \
  redis-server --port 6379 --protected-mode no \
  --appendonly yes --maxmemory "$REDIS_MAXMEMORY" --maxmemory-policy noeviction
