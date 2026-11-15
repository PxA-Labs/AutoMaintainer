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

## Database

The schema is a set of versioned migrations in
[`supabase/migrations/`](../supabase/migrations), named
`<UTC timestamp>_<name>.sql` as the Supabase CLI expects. The first one,
`20261116000000_baseline.sql`, is the whole schema as of #473; every later
change is a new file. [`supabase_schema.sql`](../supabase_schema.sql) at the
repository root is generated from them (`python scripts/db-migrate schema`)
for pasting into the SQL Editor. Do not edit it by hand. CI fails when it is
out of date.

Every migration can be applied again without harm (`IF NOT EXISTS`,
`CREATE OR REPLACE`, `DROP ... IF EXISTS` before `CREATE`). The `ci-db`
workflow applies them twice to PostgreSQL 16, and also on top of an older
schema. It checks that nothing errors, that the schema is unchanged and that
no row is lost.

### New deployment

Use one of these:

- **Supabase CLI:** `supabase link --project-ref <ref>`, then
  `supabase db push`. If the CLI asks for `supabase/config.toml`, run
  `supabase init` once first. It keeps the existing `supabase/migrations`.
- **`scripts/db-migrate`** works on any PostgreSQL, including a Supabase
  project through its direct connection string. It needs only psycopg2,
  which is a backend dependency:

  ```bash
  export DATABASE_URL='postgresql://postgres:<password>@<host>:5432/postgres'
  uv run --project backend python scripts/db-migrate up      # apply pending migrations
  uv run --project backend python scripts/db-migrate status  # applied / pending
  ```

  It applies each pending file in its own transaction, together with a row in
  `automaintainer_migrations.schema_migrations`. That schema is outside
  `public`, so the API does not expose it. A failing migration is rolled back
  and stops the run. Connect as the role that owns the schema (`postgres` on
  Supabase).
- **SQL Editor:** paste the whole of `supabase_schema.sql` and run it. This
  method does not record which migrations were applied.

Use one method per database. The CLI records versions in
`supabase_migrations.schema_migrations` and the runner records them in its own
table, so each tool would apply everything once more. That is harmless, but
it is confusing.

### Upgrading

Apply pending migrations before you roll out the new API and worker image:
`supabase db push` or `scripts/db-migrate up`. If you use the SQL Editor,
paste the new `supabase_schema.sql` again.

**A database created from the old single-file `supabase_schema.sql`** (before
#473) needs no manual steps. Back it up (Supabase dashboard, Database ->
Backups, or `pg_dump`), then apply the migrations as above. The baseline adopts
such a database: it creates only what is missing and redefines policies,
functions, triggers and views to their current form. That covers
`runs.checkpoint`, `runs.repo_name`, `repository_in_org`/`run_in_org`, the
tenant checks in the run and IDE policies, service-role-only
`github_installations` writes and the `repositories_org_immutable` trigger. It
keeps every row. These were the statements earlier upgrade notes told you to
pick out by hand. One behaviour differs from the old file. The old file
dropped and re-created the `supabase_realtime` publication. The baseline only
adds `logs`, `runs`, `webhook_events` and `ide_sessions` to it, so any other
tables you added stay.

Running the baseline takes short exclusive locks on the tables while it
replaces their policies and triggers. To record it as applied without running
it, use `supabase migration repair 20261116000000 --status applied` or
`scripts/db-migrate up --baseline`. Do that only for a database that already
has the complete current schema. Otherwise the missing pieces stay missing:
without `runs.repo_name`, for example, every `POST /start` fails.

### Changing the schema

1. Add a file: run `supabase migration new <name>`, or create
   `supabase/migrations/$(date -u +%Y%m%d%H%M%S)_<name>.sql`. Never edit a
   migration that has been released. `scripts/db-migrate up` refuses to run
   when an applied file's checksum has changed.
2. Write it so that it can run again: `IF NOT EXISTS` / `IF EXISTS`,
   `CREATE OR REPLACE`, `DROP POLICY|TRIGGER|VIEW IF EXISTS` before `CREATE`,
   and a `DO` block that checks `pg_constraint` before
   `ALTER TABLE ... ADD CONSTRAINT`. Do not write `BEGIN`/`COMMIT`.
3. Run `python scripts/db-migrate schema` and commit the regenerated
   `supabase_schema.sql`.
4. Run the database tests against a local PostgreSQL superuser:
   `DATABASE_URL_RLS_TEST=postgresql://postgres@localhost:5432/postgres uv run pytest tests/rls`
   from `backend/`. The `ci-db` workflow runs them on every PR.

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

`api`, `worker` and `beat` start only after Redis answers `redis-cli ping`
(`depends_on: condition: service_healthy`). The worker's healthcheck is a
`celery inspect ping` of its own node. If the worker is down, `POST /start`
answers `503` ("No agent worker is available") instead of queueing a run
nobody consumes; Redis itself keeps queued tasks (`appendonly yes`,
`noeviction`) across restarts.

### Smoke test

[`scripts/compose-smoke.sh`](../scripts/compose-smoke.sh) runs this topology
end to end. CI runs it on pull requests that touch the `Dockerfile`,
`docker-compose.yml` or `backend/` (workflow `compose-smoke.yml`). To run it
locally you need Docker with Compose v2.24.4 or later, `curl`, `python3`, and
host port 8000 free:

```bash
bash scripts/compose-smoke.sh                               # builds the image
COMPOSE_SMOKE_SKIP_BUILD=1 bash scripts/compose-smoke.sh    # reuse automaintainer:local
```

It starts `api`, `worker`, `beat` and `redis` under the project name
`automaintainer-smoke`, so the volumes of a dev stack are not touched. Then it
checks, in order:

1. `GET /healthz` answers `200`.
2. The worker answers `celery inspect ping`.
3. A file the worker writes to the `workspaces` volume is visible from `api`.
4. `POST /start` is accepted. `/start` returns `503` when no worker consumes
   `agent_runs`, so this proves the path from the API through Redis to the
   worker.
5. The run reaches a terminal state, and has a `celery_task_id` (set by the
   API) and a `started_at` (set by the worker when it picks the run up).

The test uses no credentials. The override file
[`scripts/compose-smoke/compose.smoke.yml`](../scripts/compose-smoke/compose.smoke.yml)
does the following, for this test only:

- It adds `fake-supabase`, an in-memory stand-in for Supabase's REST API
  (`scripts/compose-smoke/fake_supabase.py`, standard library only). It is
  seeded with one repository.
- It sets `ENVIRONMENT=development` and `AUTH_DEV_BYPASS=true`. The base
  `docker-compose.yml` keeps its production defaults.
- It sets `GITHUB_AUTH_MODE=app` without App credentials.
- It skips `backend/.env` (`env_file: !reset []`) and blanks the GitHub and
  Groq variables, so real secrets on the machine never reach the stack.

The run therefore ends `failed` with `GitHub credentials unavailable`. The
worker stops at its first GitHub step, before any call to GitHub or Groq. The
script treats exactly that outcome as success: the run went from `queued`
through the worker to a terminal state. A run that ends in `completed` needs
real GitHub and LLM credentials and is not automated.

The script always prints `docker compose ps` and the logs of every service,
then runs `docker compose down --volumes`, whether it passes or fails. The
fake server answers only the PostgREST filters it implements (`eq`, `neq`,
`is`, `in`) and returns an error for any other. If a periodic `beat` task such
as stale-run cleanup fires during the test, that error appears in the worker
log. This is expected and does not affect the result.

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
- The Python venv is installed with `uv sync --frozen --no-dev` from
  `backend/pyproject.toml` and `backend/uv.lock`, so the image gets exactly the
  locked runtime versions and the build fails if the lock is stale. uv comes
  from `ghcr.io/astral-sh/uv` (the `UV_IMAGE` build arg) and is only present in
  the builder stage. To change backend dependencies, update the lock with uv
  (see CONTRIBUTING.md, "Managing backend dependencies") and rebuild.
  No `requirements.txt` is committed; `scripts/export-backend-requirements.sh`
  exports one from the lock for platforms that need a pip requirements file.
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
| `ENABLE_TERMINAL` | unset (off) | The interactive terminal WebSocket (`/api/terminal/ws`) is refused in **every** environment unless this is `true`. Even when enabled, the shell runs as the server's OS user; its environment is scrubbed of secrets, but it is not a sandbox (it can still read files the server can). Leave it unset. |
| `TERMINAL_ADMIN_USER_IDS` | unset (nobody) | Comma-separated Supabase user IDs allowed to open the terminal when `ENABLE_TERMINAL=true`. It fails closed: empty means nobody. The check also applies under `AUTH_DEV_BYPASS`, so the dev user ID must be listed for local terminal use. |
| `GITHUB_AUTH_MODE` | `app` if App credentials are set, else `pat` | `app`: every run and IDE route acts with the GitHub App **installation token** of the repository's installation (cached, refreshed 5 minutes before expiry); a run or route with no known installation is refused, never silently downgraded to a PAT. `pat`: single-tenant mode using `GITHUB_TOKEN` for everything (a warning is logged at startup). Any other value is a startup error. |
| `ASSIST_RATE_PER_MINUTE` / `ASSIST_ORG_RATE_PER_MINUTE` / `ASSIST_DAILY_REQUESTS_PER_ORG` | `20` / `60` / `1000` | Quotas for `/assist/inline` (per user per minute, per org per minute, per org per UTC day). Exceeding one returns `429` with `Retry-After`. Counters live in Redis; without Redis they fall back to per-process memory, so limits are then per API process rather than global. |
| `WORKER_PING_TIMEOUT_SECONDS` / `WORKER_PING_CACHE_SECONDS` | `1.0` / `5.0` | `/start` pings the Celery workers first and returns `503` when none answer within the timeout, instead of queueing a run nobody will pick up. The result is cached for the given number of seconds. |
| `VERIFY_CHANGES` / `SANDBOX_BACKEND` / `VERIFY_ALLOW_LOCAL` | on / unset / unset | Agent changes are verified with the repository's tests after each push. They run through `SANDBOX_BACKEND`; running them on the host needs `VERIFY_ALLOW_LOCAL=true` in **any** environment (untrusted code). Without either, verification reports an error, which blocks `MERGE_POLICY=ci`. Timeouts, output limits and per-repository `.automaintainer.yml` are described in [VERIFICATION.md](./VERIFICATION.md). Under `ci` the GitHub App also needs the *Commit statuses: write* permission. |
| `MERGE_POLICY` | `human` | Whether the agent may merge its own PRs. `human` (default): never auto-merge; the agent opens the PR, posts its review and requests a human reviewer. `ci`: merge only if the Maintainer approves **and** every status/check on the PR head has passed, with at least one check present (evaluated once after the PR opens, so it usually fails closed). `auto`: merge on Maintainer approval alone; not recommended. Unknown values fall back to `human` with a warning. |
| `GIT_TIMEOUT_SECONDS` | `300` | Timeout for each `git clone`/`fetch` the worker runs; the git process is killed and a partial clone removed when it expires. |
| `AUTOMAINTAINER_WORKSPACE_DIR` | image: `/var/lib/automaintainer/workspaces`, code: `<tmp>/automaintainer` | Where repos are cloned. It must be the same path on the API and the `agent_runs` workers. |
| `AUTH_DEV_BYPASS` | unset | **Never set this in a deployment.** When `true`, every request is trusted as `AUTH_DEV_USER_ID` / `AUTH_DEV_ORG_ID`, with no authentication. It is only for local development. |
| `AUTH_DEV_USER_ID`, `AUTH_DEV_ORG_ID` | fixed UUIDs | Identity used by `AUTH_DEV_BYPASS`. Local only. |

### GitHub App permissions

Register the GitHub App with these permissions. Every installation must accept
them; an installation that has not granted a required permission is refused
with an error naming the missing ones, rather than failing half-way through a
run.

| Permission | Level | Why |
|---|---|---|
| Repository: *Contents* | write | Clone, read files, commit agent changes and IDE saves. |
| Repository: *Pull requests* | write | Open, review and (under `MERGE_POLICY`) merge pull requests. |
| Repository: *Issues* | write | Open issues and post the Reviewer's decision. |
| Repository: *Commit statuses* | write | Post the verification status on the agent's commit. |
| Repository: *Metadata* | read | Mandatory for every App; repository listing for sync. |
| Repository: *Checks* | read | Optional. Needed only for `MERGE_POLICY=ci`, which reads check runs; without it that merge fails closed. |
| Organization: *Members* | read | Lets the install callback and `POST /github/installations/claim` confirm that the installing user is an owner of the installing organization. |

*Workflows* is deliberately not requested, even if the App registration
grants it. Agent commits and IDE saves that touch `.github/workflows/*` are
refused by GitHub (403/404), so an agent cannot change a repository's CI.
Edit workflow files outside AutoMaintainer.

Tokens are narrower than the App registration. In `GITHUB_AUTH_MODE=app` the
backend never uses an installation's full grant:

- A run or repository route mints an installation token with exactly
  `contents`, `pull_requests`, `issues`, `statuses`: write and `metadata`:
  read, plus `checks`: read when the installation has granted it. When the
  caller names the repository (`credentials_for_installation(...,
  repository="owner/name")`), the token is limited to that one repository.
  **Not yet in effect:** the run and repository-route call sites do not pass
  a repository yet, so their tokens still cover every repository the
  installation can access (with the narrowed permissions above).
- Repository sync mints a `metadata: read` token.
- Before minting, the backend refuses an installation whose row is marked
  suspended (the `installation` suspend/deleted webhooks set
  `github_installations.suspended_at`) or that GitHub reports as suspended or
  no longer installed. No token is issued or cached for it. The webhook also
  drops cached tokens, but only in the process that handles it (normally the
  Celery worker); tokens already cached by the API server or agent processes
  remain cached for up to about an hour, and GitHub rejects them itself.

**Upgrading an existing App.** Run tokens now request every required
permission up front. An installation that has not granted one of them (for
example *Commit statuses: write*, which older registrations did not ask for)
has every run refused with an error naming the missing permission until an
owner of that account accepts the App's updated permissions on GitHub. Add
the permissions to the App registration before deploying, and ask installers
to accept them.

#### Install flow (Setup URL)

Configure the App registration:

- Enable **Request user authorization (OAuth) during installation**.
- Set the **Callback URL** and the **Setup URL** to
  `<backend origin>/github/app/setup`, and enable *Redirect on update*.

And the backend:

| Variable | Purpose |
|---|---|
| `GITHUB_APP_SLUG` | The App's URL name, for `https://github.com/apps/<slug>/installations/new`. |
| `GITHUB_APP_CLIENT_ID`, `GITHUB_APP_CLIENT_SECRET` | The App's OAuth client credentials, used to exchange the install-time code. The secret also keys the HMAC that signs the install `state`, so rotating it invalidates installs that are in progress (at most 10 minutes old). |
| `DASHBOARD_URL` | Exact dashboard origin the callback redirects to; when unset or not a bare origin, the API's own `/`. |

The flow: an org owner/admin calls `POST /github/app/install-url` with their
GitHub user token in `X-GitHub-User-Token` and is sent to the returned URL.
Its `state` is HMAC-signed, expires after 10 minutes and is bound to the
AutoMaintainer user, their org and their GitHub user id. After the install,
GitHub sends the browser to `/github/app/setup` with `installation_id`,
`setup_action`, `state` and an OAuth `code`. The route verifies the state,
re-checks the user's org role, exchanges the code for the installer's user
token, and runs the same ownership check and write as
`POST /github/installations/claim` (the installer must be the GitHub user the
state names). Only then is the installation linked. It redirects to
`DASHBOARD_URL` with `github_setup=<outcome>` (`linked`, `unverified`,
`invalid`, `expired`, `forbidden`, `unauthorized`, `conflict`, `requested`,
`unavailable` or `error`) and, when linked, `github_installation_id`. The
`installation_id` in the URL is never trusted on its own; a callback without
`state` and `code` links nothing (`unverified`), and the installation can
still be linked with `POST /github/installations/claim`.

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
- [ ] `MERGE_POLICY` is unset or `human`, and branch protection with required checks is on for maintained repos.
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
on PRs that touch it. The `compose-smoke` workflow runs the whole Compose
stack and drives one run from `queued` to a terminal state (see
[Smoke test](#smoke-test)). That run ends `failed` for lack of credentials. A
run that reaches `completed` needs real GitHub and LLM credentials, and is
not automated.

## Upgrade notes

- **Database: versioned migrations (#473).** The schema is now applied from `supabase/migrations`, and `supabase_schema.sql` is generated from them. For an existing database, apply the migrations once as described in [Database, Upgrading](#upgrading). This replaces the statements that earlier notes asked you to run by hand: `runs.checkpoint` (run state checkpoints, so a redelivered run resumes instead of repeating GitHub side effects), `runs.repo_name` (without it every `POST /start` insert fails), the tenant checks in the run and IDE policies, the `github_installations` policies and the repository org-immutability trigger. All rows are kept.
- **Webhook processing:** stored `webhook_events` are now processed by a Celery task (installation deleted/suspended, repositories added/removed). These tasks are routed to the `github_sync` queue (`celery_app.py`), so make sure a worker consumes it.
- **GitHub App installations (multi-tenant):** the database part is in the baseline migration (see above). Authenticated users can no longer insert installation rows directly. An org owner/admin links an installation with `POST /github/installations/claim {installation_id}` and an `X-GitHub-User-Token` header (the Supabase GitHub provider token); the backend verifies with GitHub that the user can access the installation and administers its account. The App needs the *Members: read* organisation permission for that check. Repository sync never moves a repository that already belongs to another org.

- **Supabase redirect URL:** add `<your-origin>/auth/callback/` to the Supabase Auth allowed redirect URLs before deploying the dashboard. GitHub sign-in now returns to that page.
- **Workspace directory names changed** to `owner_repo-<hash>` so different repositories can never share a clone. Existing clones under the old names are not reused; each repository is cloned again on its next run. Old directories can be deleted once the new ones exist.
- **Workspace directories keyed by repository id:** agent runs and the IDE/file/terminal routes now share a clone at `_by_id/<GitHub repository id>` in the workspace, so a renamed repository keeps its clone and a recreated one gets a fresh one. Existing clones move there on first use (one re-clone per repository); the old `owner_repo-<hash>` directories can then be deleted.
- **Clone credentials:** the worker no longer writes the GitHub token into `.git/config`, and it scrubs `http.extraheader` from existing clones on first use. If a token was ever stored there, rotate it.
