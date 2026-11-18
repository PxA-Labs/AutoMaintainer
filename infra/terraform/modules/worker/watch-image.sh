#!/bin/bash
# Restarts the worker when the "automaintainer-image" metadata key changes
# (the deploy workflow's "roll the worker" step). The restart stops the old
# container with a grace period, so Celery finishes or requeues its tasks.
set -uo pipefail

md_url="http://metadata.google.internal/computeMetadata/v1/instance/attributes/automaintainer-image"
get() { curl -fsS -H 'Metadata-Flavor: Google' "$md_url" 2>/dev/null; }

current="$(get || true)"
while true; do
  sleep 20
  latest="$(get)" || continue
  if [ -n "$latest" ] && [ "$latest" != "$current" ]; then
    echo "worker image changed to $latest; restarting the worker"
    systemctl restart automaintainer-worker.service
    current="$latest"
  fi
done
