# Deploying AutoMaintainer

This guide covers the supported ways to run AutoMaintainer outside a dev
laptop: Docker Compose, Render, and a single container (for example Hugging
Face Spaces). It lists every environment variable the backend reads and the
security rules for production.

## Topology

AutoMaintainer has four runtime pieces:

- **API**: FastAPI. It serves the REST/WebSocket API and, from the Docker image, the static dashboard.
- **Agent worker**: a Celery worker on the `agent_runs` queue. It clones target repos and runs the LangGraph agents.
- **Maintenance worker and beat**: Celery on `maintenance` and `github_sync`, plus the periodic scheduler.
- **Redis**: the Celery broker and result backend, also used for repo locks and rate limiting.

Supabase (Postgres and Auth), GitHub and Groq are external services.

```
              browser (dashboard: Vercel, or served by the API)
                     │  HTTPS / WSS
                     ▼
 ┌──────────────────────────────────────────┐        ┌───────────────┐
 │ API (uvicorn main:app)                   │◄──────►│ Supabase      │
 │  /start ─► enqueue ──────────┐           │        │ (DB + Auth)   │
 │  /repo/.../tree|file|search  │ reads     │        └───────────────┘
 │  /api/terminal/ws (opt-in)   │ clones    │               ▲
 └──────────────┬───────────────┼───────────┘               │
                │               ▼                           │
                │   ┌─────────────────────────┐             │
                │   │ WORKSPACE DIR (shared)  │             │
                │   │ AUTOMAINTAINER_         │             │
                │   │   WORKSPACE_DIR         │             │
                │   └─────────────────────────┘             │
                │               ▲ clones / writes           │
                ▼               │                           │
 ┌──────────────────┐  ┌────────┴─────────────────┐         │
 │ Redis            │◄►│ Celery worker            │─────────┤
 │ broker / locks   │  │ -Q agent_runs            │──► GitHub, Groq
 └──────────────────┘  └──────────────────────────┘         │
          ▲            ┌──────────────────────────┐         │
          └───────────►│ Celery worker + beat     │─────────┘
                       │ -Q maintenance,github_sync│
                       └──────────────────────────┘
```

**Invariant: the API and every `agent_runs` worker must see the same
`AUTOMAINTAINER_WORKSPACE_DIR`.** The worker clones repositories there, and
the API's IDE routes and terminal read them back from it. If they do not share
it, the IDE returns `404 Repository not cloned yet` for repos the agent has
just processed. Maintenance and beat never touch clones.

## Option A: Docker Compose (local or a single prod-like host)

```bash
# 1. Create backend/.env with the variables below (at least SUPABASE_*, GROQ_API_KEY, GITHUB_*)
# 2. Optional: export the public dashboard build values
export NEXT_PUBLIC_SUPABASE_URL=https://<project>.supabase.co
export NEXT_PUBLIC_SUPABASE_ANON_KEY=<anon key>
# 3. Start everything
docker compose up --build
# API + dashboard: http://localhost:8000   liveness: http://localhost:8000/healthz
```

[`docker-compose.yml`](../docker-compose.yml) starts `api`, `worker`, `beat`
and `redis`. `api` and `worker` mount the named volume `workspaces` at
`/var/lib/automaintainer/workspaces`. To add agent capacity, run
`docker compose up --scale worker=3`; all replicas share the volume. Run only
one `beat`.

Compose sets `ENVIRONMENT=production` unless you export
`ENVIRONMENT=development`. The `env_file` `required: false` syntax needs
Docker Compose v2.24 or later.

## Option B: Render

[`render.yaml`](../render.yaml) is a Blueprint with three services:

| Service | Type | Runs |
|---|---|---|
| `automaintainer-backend` | web, Docker, **1 instance** | `automaintainer-api-and-worker`: uvicorn plus the `agent_runs` Celery worker in one container |
| `automaintainer-celery-maintenance` | worker, Docker, 1 instance | `celery worker -Q maintenance,github_sync -B` |
| `automaintainer-redis` | Redis (`noeviction`) | broker, results, locks |

**Why the API and the agent worker share one service.** A Render persistent
disk attaches to exactly one service and cannot be shared, and services do not
share a filesystem. With a separate worker service, its clones would never be
visible to the API. Running both processes in one container is the simplest
correct layout. It has three consequences:

- The backend service must stay at one instance, because each instance has
  its own filesystem. Scale agent throughput with `CELERY_CONCURRENCY` or a
  larger plan.
- Clones live on the container's ephemeral filesystem. They are lost on
  deploy or restart and re-cloned on the next run. No disk is attached: Render
  mounts disks root-owned, and the image runs as a non-root user.
- The launcher exits if either process dies, so Render restarts the whole
  service. SIGTERM is forwarded to both processes, and Celery's `acks_late`
  requeues an interrupted run.

The long-term fix, which lets workers scale independently, is for the IDE
routes to read files through the GitHub API instead of local clones (epic
#418).

**Migrating from the old blueprint:** delete the old
`automaintainer-celery-worker` service if a Blueprint sync leaves it in place.
Otherwise it keeps consuming `agent_runs` into its own, unshared filesystem.

All services build the repository `Dockerfile`, because Render's native Python
runtime has no Node or gitnexus. Set every `sync: false` variable in the Render
dashboard.

## Option C: a single container (Hugging Face Spaces, plain `docker run`)

The image's default command starts only the API. A single-container platform
also needs an agent worker and an external Redis:

```bash
docker run -p 7860:7860 --init \
  -e REDIS_URL=rediss://... -e SUPABASE_URL=... -e SUPABASE_SERVICE_KEY=... \
  -e GROQ_API_KEY=... -e GITHUB_TOKEN=... \
  ghcr.io/pxa-labs/automaintainer:latest automaintainer-api-and-worker
```

If you run the API without any worker, `POST /start` enqueues runs that stay
`queued` forever. Periodic maintenance (stale-run cleanup, repo sync) also
needs a beat process. On a single container, add `CELERY_QUEUES=agent_runs,maintenance,github_sync`
and run `celery -A celery_app beat -s /tmp/celerybeat-schedule` somewhere, for
example as a second Space.

## The image

- Multi-stage build: the Next.js export, Node tools, the Python venv and the
  runtime are separate stages. Compilers stay in the builder stages.
- Base images are pinned to `node:22.23.5-bookworm-slim` and
  `python:3.11.18-slim-bookworm`. gitnexus is pinned to `1.6.12` (it needs Node
  `^22.18 || >=24.11`). Bump these through the `ARG`s at the top of the
  Dockerfile.
- Runs as the unprivileged user `app` (uid 10001). Application code is
  read-only for that user. Only `$HOME` and `/var/lib/automaintainer/workspaces`
  are writable.
- `ENVIRONMENT=production` and
  `AUTOMAINTAINER_WORKSPACE_DIR=/var/lib/automaintainer/workspaces` are baked
  in.
- `HEALTHCHECK` calls `GET /healthz`, a liveness check that does not depend on
  Supabase or Redis. Use `/healthz/supabase` for readiness and monitoring, not
  as a restart trigger. Worker and beat containers reuse the image, so they
  must override or disable the healthcheck (Compose does this).
- `NEXT_PUBLIC_SUPABASE_URL`, `NEXT_PUBLIC_SUPABASE_ANON_KEY` and
  `NEXT_PUBLIC_BACKEND_URL` are build args, inlined into the static dashboard.
  They are public values. Never pass secrets as build args.

## Environment variables

### Required (API and workers)

| Variable | Purpose |
|---|---|
| `SUPABASE_URL` | Supabase project URL. |
| `SUPABASE_SERVICE_KEY` | Supabase **service-role** key. It bypasses RLS, so keep it server-side only. |
| `GROQ_API_KEY` | LLM provider key used by the agents. |
| `REDIS_URL` | Celery broker and result backend, locks and rate limits. Use `rediss://` for TLS. |
| `GITHUB_TOKEN` *or* `GITHUB_APP_ID` + `GITHUB_APP_PRIVATE_KEY` (or `GITHUB_APP_PRIVATE_KEY_PATH`) | GitHub access. Prefer the GitHub App, which has short-lived installation tokens. |
| `GITHUB_WEBHOOK_SECRET` | Verifies `POST /webhooks/github` signatures. Required if webhooks are configured. |
| `ALLOWED_ORIGINS` (alias `CORS_ORIGINS`) | Comma-separated exact dashboard origins. Used for CORS and the terminal WebSocket origin check. Wildcards are rejected. |

### Deployment and security

| Variable | Default | Purpose |
|---|---|---|
| `ENVIRONMENT` | `production` in the image, `development` in the code | Must be `production` in any deployment. It closes production-only gates such as the terminal. |
| `ENABLE_TERMINAL` | unset (off) | When `ENVIRONMENT=production`, the interactive terminal WebSocket (`/api/terminal/ws`) is refused unless this is `true`. The terminal is a shell with the server's privileges and environment. Leave it unset. |
| `TERMINAL_ADMIN_USER_IDS` | unset (nobody) | Comma-separated Supabase user IDs allowed to open the terminal, even with `ENABLE_TERMINAL=true`. It fails closed: empty means nobody. This allowlist is added by the terminal-hardening work (see the terminal security issue). If your backend version does not read it yet, org membership is the only check, so keep `ENABLE_TERMINAL` unset. |
| `MERGE_POLICY` | unset (open PRs only, no auto-merge) | Controls whether the agent may merge its own PRs. Leave it unset for the safe default, where the agent only opens PRs and branch protection or reviewers decide. It is introduced by the auto-merge gating work. Check that work for the accepted values before you set it. |
| `AUTOMAINTAINER_WORKSPACE_DIR` | image: `/var/lib/automaintainer/workspaces`, code: `<tmp>/automaintainer` | Where repos are cloned. It must be the same path on the API and the `agent_runs` workers. |
| `AUTH_DEV_BYPASS` | unset | **Never set this in a deployment.** When `true`, every request is trusted as `AUTH_DEV_USER_ID` / `AUTH_DEV_ORG_ID`, with no authentication. It is only for local development. |
| `AUTH_DEV_USER_ID`, `AUTH_DEV_ORG_ID` | fixed UUIDs | Identity used by `AUTH_DEV_BYPASS`. Local only. |

### Tuning and observability (optional)

| Variable | Default | Purpose |
|---|---|---|
| `PORT` | `7860` (image) | HTTP port. Render injects its own. |
| `CELERY_QUEUES` | `agent_runs` | Queues consumed by `automaintainer-api-and-worker`. |
| `CELERY_CONCURRENCY` | `2` | Worker processes in `automaintainer-api-and-worker`. |
| `CELERY_BROKER_POOL_LIMIT` | `10` | Broker connections per worker process. |
| `REDIS_SSL_CERT_REQS` | `required` | TLS verification for `rediss://`. Keep it `required`. |
| `REPO_LOCK_TTL_SECONDS` | `1800` | TTL of the per-repo run lock. |
| `GROQ_REQUESTS_PER_MINUTE` / `GROQ_TOKENS_PER_MINUTE` | `30` / `6000` | Client-side LLM rate limits. |
| `CORS_ORIGIN_REGEX` | built-in default | Extra origin regex for CORS. |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | unset | OTLP traces and metrics endpoint. |

### Dashboard (build time, public)

`NEXT_PUBLIC_SUPABASE_URL`, `NEXT_PUBLIC_SUPABASE_ANON_KEY` (the **anon** key,
never the service key), and `NEXT_PUBLIC_BACKEND_URL`.

## Security checklist

- [ ] `ENVIRONMENT=production` on every service.
- [ ] `AUTH_DEV_BYPASS` is absent everywhere. Check the platform dashboard, not only the repo files.
- [ ] `ENABLE_TERMINAL` is unset unless you need it. If it is set, `TERMINAL_ADMIN_USER_IDS` lists only operators.
- [ ] `MERGE_POLICY` is unset (PR-only), and branch protection with required checks is on for maintained repos.
- [ ] `ALLOWED_ORIGINS` lists only your dashboard origins.
- [ ] Use the GitHub App (short-lived installation tokens) rather than a long-lived PAT in `GITHUB_TOKEN`.
- [ ] Redis is not exposed publicly. Compose does not publish it, and on Render `ipAllowList: []` allows internal access only.
- [ ] **Rotate keys** (`SUPABASE_SERVICE_KEY`, `GROQ_API_KEY`, `GITHUB_TOKEN` or the App private key, `GITHUB_WEBHOOK_SECRET`) on a schedule, and immediately if any of these happens: the terminal was ever enabled, `AUTH_DEV_BYPASS` was ever set on a reachable deployment, or a secret appeared in logs, images or `.git/config` of a clone. After rotating, redeploy every service, because each one holds its own copy.
- [ ] Never bake secrets into the image. `.dockerignore` excludes `**/.env`. Pass secrets at runtime only.

## What was and was not verified

The Dockerfile, the Compose file and the Render Blueprint are covered by static
tests in `backend/tests/test_deployment_config.py`. Those tests check the
non-root user, `ENVIRONMENT=production`, pinned versions, the absence of a
toolchain in the runtime stage, the shared workspace volume, and that no
dangerous flags are set. The `docker-publish` workflow also builds the image
on PRs that touch it. A full `docker compose up` end-to-end run, from `queued`
to `completed`, has not yet been automated.
