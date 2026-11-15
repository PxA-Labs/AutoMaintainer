#!/usr/bin/env bash
# Export backend runtime dependencies from backend/uv.lock as a pip
# requirements file, for tools or platforms that only understand pip.
#
#   bash scripts/export-backend-requirements.sh                 # print to stdout
#   bash scripts/export-backend-requirements.sh requirements.txt  # write a file
#
# backend/pyproject.toml + backend/uv.lock are the source of truth; no
# requirements file is committed (it would go stale on every Dependabot PR).
# Requires uv on PATH.
set -euo pipefail

cd "$(dirname "$0")/../backend"

args=(export --frozen --no-dev --no-emit-project --no-hashes --format requirements-txt)
if [[ $# -gt 0 ]]; then
  # Redirect instead of -o: uv -o also echoes to stdout and embeds the path.
  uv "${args[@]}" > "$1"
  echo "Wrote $1" >&2
else
  uv "${args[@]}"
fi
