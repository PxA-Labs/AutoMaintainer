# AutoMaintainer image: FastAPI API (also serves the static dashboard), Celery
# worker/beat (same image, different command) and the gitnexus CLI.
#
# Base images are pinned to exact versions on the same Debian release
# (bookworm) so that native Node/Python modules built in the builder stages
# link against the same glibc as the runtime stage. Bump them together.
ARG NODE_IMAGE=node:22.23.5-bookworm-slim
ARG PYTHON_IMAGE=python:3.11.18-slim-bookworm
# uv installs the backend venv from backend/uv.lock. Keep the version in step
# with UV_VERSION in .github/workflows/ci-backend.yml.
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.13.8

# ---------------------------------------------------------------------------
# Stage 1: build the Next.js static export (dashboard/out)
# ---------------------------------------------------------------------------
FROM ${NODE_IMAGE} AS frontend-builder
WORKDIR /app/dashboard
# NEXT_PUBLIC_* values are inlined into the static bundle at build time. They
# are public by design (anon key, URLs) - never pass server secrets here.
ARG NEXT_PUBLIC_SUPABASE_URL=""
ARG NEXT_PUBLIC_SUPABASE_ANON_KEY=""
ARG NEXT_PUBLIC_BACKEND_URL=""
ENV NEXT_PUBLIC_SUPABASE_URL=${NEXT_PUBLIC_SUPABASE_URL} \
    NEXT_PUBLIC_SUPABASE_ANON_KEY=${NEXT_PUBLIC_SUPABASE_ANON_KEY} \
    NEXT_PUBLIC_BACKEND_URL=${NEXT_PUBLIC_BACKEND_URL} \
    NEXT_TELEMETRY_DISABLED=1
COPY dashboard/package.json dashboard/package-lock.json ./
RUN npm ci
COPY dashboard/ ./
RUN npm run build

# ---------------------------------------------------------------------------
# Stage 2: install the Node CLI tools used by the agents (gitnexus).
# Uses the official Node image instead of `curl | bash` NodeSource setup.
# gitnexus 1.6.x requires Node ^22.18.0 || >=24.11.0.
# Compilers are only present in this stage, for packages that build native
# addons with node-gyp when no prebuilt binary matches.
# ---------------------------------------------------------------------------
FROM ${NODE_IMAGE} AS node-tools
ARG GITNEXUS_VERSION=1.6.12
ARG TREE_SITTER_KOTLIN_VERSION=0.3.8
RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 make g++ \
    && rm -rf /var/lib/apt/lists/*
RUN npm install -g --omit=dev \
        "gitnexus@${GITNEXUS_VERSION}" \
        "tree-sitter-kotlin@${TREE_SITTER_KOTLIN_VERSION}" \
    && npm cache clean --force

# ---------------------------------------------------------------------------
# Stage 3: build the Python virtualenv (build-essential stays in this stage)
# Installs exactly what backend/uv.lock pins (engine deps plus the server
# extra: FastAPI, uvicorn, Celery, OpenTelemetry; no dev
# group; other extras only through the EXTRAS build arg below). --frozen installs the lock as-is and never re-resolves (Backend CI
# runs `uv lock --check` so the lock always matches pyproject.toml). The venv
# is created at /opt/venv with the base image's interpreter, which sits at the
# same path in the runtime stage.
# ---------------------------------------------------------------------------
FROM ${UV_IMAGE} AS uv-bin

FROM ${PYTHON_IMAGE} AS python-builder
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*
COPY --from=uv-bin /uv /usr/local/bin/uv
ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_PYTHON=/usr/local/bin/python3 \
    UV_PYTHON_DOWNLOADS=never \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_NO_CACHE=1
WORKDIR /build
COPY backend/pyproject.toml backend/uv.lock ./
# EXTRAS: comma-separated optional dependency groups of backend/pyproject.toml.
# The server extra is always installed; "server,rag" adds the Vertex AI
# client code retrieval needs (docs/RAG.md), "adk" the ADK Reviewer. Any
# other name fails the build. The deploy workflow passes the repository
# variable IMAGE_EXTRAS.
ARG EXTRAS="server"
RUN set -eu; \
    extras=""; \
    for e in $(echo "$EXTRAS" | tr ',' ' '); do \
        case "$e" in \
            server) ;; \
            rag|adk) extras="$extras --extra $e" ;; \
            *) echo "Unknown extra in EXTRAS: $e" >&2; exit 1 ;; \
        esac; \
    done; \
    uv sync --frozen --no-dev --no-install-project --extra server $extras

# ---------------------------------------------------------------------------
# Stage 4: runtime
# ---------------------------------------------------------------------------
FROM ${PYTHON_IMAGE} AS runtime

# git: cloning/committing target repos. bash: terminal PTY and the
# api+worker launcher below. No compilers, no curl.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git bash ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Node runtime + globally installed CLI tools from the node-tools stage.
# The npm/npx/gitnexus entries in /usr/local/bin are symlinks, which COPY
# would dereference, so they are recreated explicitly.
COPY --from=node-tools /usr/local/bin/node /usr/local/bin/node
COPY --from=node-tools /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
    && ln -s ../lib/node_modules/gitnexus/dist/cli/index.js /usr/local/bin/gitnexus \
    && node --version && gitnexus --help >/dev/null

COPY --from=python-builder /opt/venv /opt/venv

# Unprivileged runtime user. Application code stays root-owned (read-only for
# the process); only HOME and the workspace directory are writable.
ARG APP_UID=10001
RUN groupadd --system --gid ${APP_UID} app \
    && useradd --system --uid ${APP_UID} --gid app --create-home --home-dir /home/app --shell /bin/bash app \
    && mkdir -p /var/lib/automaintainer/workspaces \
    && chown -R app:app /var/lib/automaintainer

WORKDIR /app
COPY backend/ ./backend/
COPY --from=frontend-builder /app/dashboard/out ./dashboard/out

# Launcher for single-container platforms (Render web service, Hugging Face
# Spaces) where the API and the agent_runs worker must share one filesystem:
# the worker clones repos into AUTOMAINTAINER_WORKSPACE_DIR and the API's IDE
# routes read them from there. If either process exits, the other is stopped
# and the container exits non-zero so the platform restarts it. SIGTERM is
# forwarded so Celery can finish/requeue (acks_late) on deploys.
RUN printf '%s\n' \
    '#!/bin/bash' \
    'set -u' \
    'trap "kill -TERM \$(jobs -p) 2>/dev/null" TERM INT' \
    'celery -A celery_app worker -Q "${CELERY_QUEUES:-agent_runs}" -l info --concurrency="${CELERY_CONCURRENCY:-2}" &' \
    'uvicorn main:app --host 0.0.0.0 --port "${PORT:-7860}" &' \
    'wait -n' \
    'rc=$?' \
    'kill -TERM $(jobs -p) 2>/dev/null' \
    'wait' \
    'exit $(( rc == 0 ? 1 : rc ))' \
    > /usr/local/bin/automaintainer-api-and-worker \
    && chmod 0755 /usr/local/bin/automaintainer-api-and-worker

# ENVIRONMENT=production is the safe default: it keeps production-only gates
# (e.g. the interactive terminal stays disabled unless ENABLE_TERMINAL=true)
# closed. Override to "development" only for local work.
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    ENVIRONMENT=production \
    AUTOMAINTAINER_WORKSPACE_DIR=/var/lib/automaintainer/workspaces \
    HOST=0.0.0.0 \
    PORT=7860

# Hugging Face Spaces expects 7860; Render injects its own PORT.
EXPOSE 7860

USER app
WORKDIR /app/backend

# Liveness only (/healthz does not touch Supabase or Redis). Uses Python
# because curl is not installed. Worker/beat containers reuse this image and
# must override or disable the healthcheck (see docker-compose.yml).
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('PORT', '7860'), timeout=4)"]

# Run the FastAPI backend (which also serves the static dashboard). Shell form
# via `sh -c` so $PORT from the platform is honoured; `exec` keeps uvicorn as
# PID 1 so it receives SIGTERM directly.
CMD ["sh", "-c", "exec uvicorn main:app --host 0.0.0.0 --port \"${PORT:-7860}\""]
