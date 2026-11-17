#!/bin/bash
# Runs the AutoMaintainer Celery worker (all queues) and beat in the image
# named by the instance metadata key "automaintainer-image". The deploy
# workflow changes that key; watch-image.sh then restarts this unit.
set -euo pipefail

# shellcheck source=/dev/null
source /etc/automaintainer/worker.env

md_url="http://metadata.google.internal/computeMetadata/v1/instance/attributes/automaintainer-image"
image="$(curl -fsS -H 'Metadata-Flavor: Google' "$md_url" || true)"
if [ -z "$image" ]; then
  echo "automaintainer-image is not set yet; waiting for the first deploy" >&2
  exit 75
fi

docker-credential-gcr configure-docker --registries="$REGISTRY_HOST" >/dev/null
docker pull "$image"
docker rm -f automaintainer-worker >/dev/null 2>&1 || true

# --network host: reaches Redis on 127.0.0.1 (redis_mode "vm") and the
# metadata server for secrets_exec.py. The workspaces volume keeps clones
# across restarts. The image's HEALTHCHECK probes the API port, which a
# worker does not serve, hence --no-healthcheck.
exec docker run --rm --name automaintainer-worker \
  --init --network host --no-healthcheck \
  --env-file /etc/automaintainer/app.env \
  -v automaintainer-workspaces:/var/lib/automaintainer/workspaces \
  -v /etc/automaintainer/secrets_exec.py:/opt/automaintainer/secrets_exec.py:ro \
  "$image" \
  python /opt/automaintainer/secrets_exec.py \
  celery -A celery_app worker -Q "$CELERY_QUEUES" -l info \
  --concurrency="$CELERY_CONCURRENCY" -B -s /tmp/celerybeat-schedule
