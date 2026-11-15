#!/usr/bin/env bash
# Regenerate backend/requirements.txt from backend/uv.lock, or verify it.
#
#   bash scripts/export-backend-requirements.sh          # rewrite the file
#   bash scripts/export-backend-requirements.sh --check  # exit 1 if stale
#
# backend/pyproject.toml + backend/uv.lock are the source of truth. The
# requirements.txt export (runtime dependencies only, universal markers) exists
# for tools and platforms that only understand pip requirements files.
# Requires uv on PATH.
set -euo pipefail

cd "$(dirname "$0")/../backend"

render() {
  cat <<'EOF'
# GENERATED FILE - DO NOT EDIT BY HAND.
# Exported from backend/uv.lock (runtime dependencies only). Change
# dependencies in backend/pyproject.toml with `uv add` / `uv lock`, then run:
#    bash scripts/export-backend-requirements.sh
# CI fails if this file is out of sync with uv.lock.
EOF
  uv export --frozen --no-dev --no-emit-project --no-hashes --no-header \
    --format requirements-txt
}

case "${1:-}" in
  --check)
    # The lock must match pyproject.toml, and the export must match the lock.
    uv lock --check
    if ! diff -u requirements.txt <(render); then
      echo "backend/requirements.txt is out of sync with backend/uv.lock." >&2
      echo "Run: bash scripts/export-backend-requirements.sh" >&2
      exit 1
    fi
    echo "backend/requirements.txt is in sync with backend/uv.lock."
    ;;
  "")
    render > requirements.txt
    echo "Wrote backend/requirements.txt"
    ;;
  *)
    echo "usage: $0 [--check]" >&2
    exit 2
    ;;
esac
