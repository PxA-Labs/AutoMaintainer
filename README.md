<div align="center">
  
# AutoMaintainer
**A multi-agent pipeline that turns repository issues into reviewed GitHub pull requests**

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](https://opensource.org/licenses/MIT)
[![OpenSSF Scorecard](https://api.securityscorecards.dev/projects/github.com/PxA-Labs/AutoMaintainer/badge)](https://securityscorecards.dev/viewer/?uri=github.com/PxA-Labs/AutoMaintainer)
[![CI Backend](https://github.com/PxA-Labs/AutoMaintainer/actions/workflows/ci-backend.yml/badge.svg)](https://github.com/PxA-Labs/AutoMaintainer/actions/workflows/ci-backend.yml)
[![CodeQL](https://github.com/PxA-Labs/AutoMaintainer/actions/workflows/codeql-analysis.yml/badge.svg)](https://github.com/PxA-Labs/AutoMaintainer/actions/workflows/codeql-analysis.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/release/python-3110/)
[![Next.js](https://img.shields.io/badge/Next.js-16-black.svg)](https://nextjs.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-backend-green.svg)](https://fastapi.tiangolo.com/)

</div>

AutoMaintainer runs a pipeline of LLM agents against a GitHub repository. The
agents pick or receive an issue, decide whether to build it, change one file,
open a pull request and review it. By default the agents never merge their
own pull requests; a human does.

It is built with **LangGraph**, **FastAPI**, **Celery + Redis**, **Supabase**
and **Next.js**. The model is **Llama 3.3 70B on Groq**; on the main
(GitNexus tool) path a failed 70B call silently falls back to **Llama 3.1 8B**,
so reviews and code may come from the smaller model. See
[ARCHITECTURE.md](./ARCHITECTURE.md) for how the pieces fit together.

---

> [!WARNING]
> ### Project status: beta (pre-1.0)
> The multi-tenant migration has landed: Celery workers, Supabase Auth with
> organization-scoped data and Row Level Security, and GitHub App installation
> tokens. Every API route except the health probes, the OpenAPI docs and the
> signed GitHub webhook requires a Supabase login, and a test enforces this.
> When the API serves the static dashboard build at `/`, those files are public
> too. They hold no user data, only the compiled UI and its public
> `NEXT_PUBLIC_*` settings (backend URL, Supabase URL and anon key).
> Limitations that remain:
> * **No sandbox ships with the project.** Agent changes are verified with
>   the target repository's tests only once `SANDBOX_BACKEND` is configured
>   (or `VERIFY_ALLOW_LOCAL=true` on a machine you trust; see
>   [docs/VERIFICATION.md](./docs/VERIFICATION.md)). Until then verification
>   reports an error, `MERGE_POLICY=ci` never merges, and the Maintainer's
>   review is the LLM's alone. Keep the default `MERGE_POLICY=human`.
> * **One file per change.** The Implementer rewrites a single file per run.
> * **Single-host deployment.** The API and the agent worker must share the
>   clone directory, so the Render blueprint runs both in one instance.
> * **The interactive terminal is not a sandbox.** It is off by default. Do
>   not enable it on a deployment that other people can reach.
>
> It is suitable for running against your own repositories. Hosting it for
> untrusted tenants is not recommended yet. Open work is tracked in the
> [epics](https://github.com/PxA-Labs/AutoMaintainer/issues?q=is%3Aissue+is%3Aopen+label%3Aepic).

---

> [!IMPORTANT]
> ### Supabase is required
> AutoMaintainer stores organizations, runs and logs in **Supabase** (Postgres,
> Auth and Realtime). Provision your own project:
> * Create a project at [supabase.com](https://supabase.com).
> * Apply the migrations in [`supabase/migrations`](./supabase/migrations)
>   with `supabase db push` or `python scripts/db-migrate up`, or paste the
>   generated [`supabase_schema.sql`](./supabase_schema.sql) into the
>   **Supabase SQL Editor**. They create `organizations`,
>   `organization_members`, `github_installations`, `repositories`, `runs`,
>   `logs`, `usage_events`, `webhook_events` and `ide_sessions`, with RLS
>   policies. All of them can be re-run, and upgrades keep your data
>   ([Database setup and upgrades](./docs/DEPLOYMENT.md#database)).
> * Put your credentials in `backend/.env` and `dashboard/.env.local`.
>
> If Supabase is unreachable or paused, the dashboard shows a warning banner,
> and run history and live logs are unavailable.

---

## Features
Each item names the code it is based on.

- **Five-agent LangGraph pipeline** (`backend/agents.py`): Architect, Visionary, Reviewer/PM, Implementer and Maintainer. If the Maintainer rejects a change, the Implementer revises it, up to three Implementer attempts in total.
- **Two modes**: autonomous (the Architect proposes the work) or targeted (`target_issue`: work on an existing issue). The issue must name the file to change.
- **Works through GitHub**: issues, issue comments, branches, commits, pull requests and review comments, using the repository's GitHub App installation token (`backend/github_app.py`).
- **Human-gated merging**: `MERGE_POLICY=human` (the default) never merges. `ci` merges only when the checks on the PR head pass (see the merge-policy table in [ARCHITECTURE.md](ARCHITECTURE.md#merge-policy); no checks at all counts as failing). `ci` also requires the change to pass the repository's own tests (the `automaintainer/verification` status; see [docs/VERIFICATION.md](./docs/VERIFICATION.md)). The PM and Maintainer return JSON decisions validated against a schema; anything that does not validate after one retry counts as rejection.
- **Durable runs**: a Celery worker runs each job, with a per-repository Redis lock, heartbeats, stale-run cleanup and cancellation (`backend/tasks.py`). A run that a crashed worker delivers again resumes from `runs.checkpoint`, without duplicate issues, branches or PRs.
- **Prompt-injection hardening**: repository and issue text reach the model inside delimited untrusted-data blocks.
- **Code context**: an AST map of the clone (tree-sitter) and GitNexus tools over MCP. GitNexus runs locally. Prompts, including repository content, are sent to Groq.
- **Dashboard** (`dashboard/`): start and stop runs, live logs through Supabase Realtime, run history and an org admin page.
- **Web IDE**: a Monaco editor over the repository clone, inline AI assist (rate-limited per user and per organization) and a "propose changes" flow that opens a PR. Saving a single file commits directly to the default branch.
- **Opt-in admin terminal**: a PTY over WebSocket, off unless `ENABLE_TERMINAL=true`, and limited to `TERMINAL_ADMIN_USER_IDS`.
- **Offline eval harness** (`backend/evals/`, [docs/EVALS.md](./docs/EVALS.md)): scores the Implementer step on 10 synthetic tasks with hidden tests. No benchmark results are published yet.
- **Deployment configs**: Docker image (non-root), Docker Compose, a Render blueprint, and a static dashboard export for Vercel. See [docs/DEPLOYMENT.md](./docs/DEPLOYMENT.md).

No end-to-end latency or success-rate figures are claimed. None have been
measured.

---

## Quickstart

- **Run it**: follow [docs/DEPLOYMENT.md](./docs/DEPLOYMENT.md). `docker compose up --build` starts the API, a worker, beat and Redis. You still need Supabase, a Groq key and GitHub credentials.
- **Develop it**: follow the setup below and [CONTRIBUTING.md](./CONTRIBUTING.md).

---

## Development & Local Setup

### 1. Prerequisites
- [Node.js](https://nodejs.org/en/) (v22.18+; the gitnexus CLI requires `^22.18 || >=24.11`)
- [Python](https://www.python.org/) (3.11+)
- A [Groq API Key](https://console.groq.com/keys)
- A [GitHub Token](https://github.com/settings/tokens) or GitHub App credentials
- A [Supabase Project](https://supabase.com/) (Free Tier supported)
- [Redis](https://redis.io/) (optional, required for Celery task workers)

### 2. Environment Configuration

Clone the repository:
```bash
git clone https://github.com/PxA-Labs/AutoMaintainer.git
cd AutoMaintainer
```

**Database Migration:** apply [`supabase/migrations`](./supabase/migrations)
with the Supabase CLI (`supabase link`, then `supabase db push`) or with
`DATABASE_URL=... uv run --project backend python scripts/db-migrate up`.
Without either tool, open your [Supabase SQL Editor](https://database.new)
and run the entire generated [`supabase_schema.sql`](./supabase_schema.sql).
Use the same method for every upgrade. See
[docs/DEPLOYMENT.md](./docs/DEPLOYMENT.md#database).

**Backend Environment (`backend/.env`):**
```bash
GROQ_API_KEY="your_groq_api_key_here"
GITHUB_TOKEN="your_github_token_here"
SUPABASE_URL="https://your-project.supabase.co"
SUPABASE_SERVICE_KEY="your_supabase_service_role_key"
REDIS_URL="redis://localhost:6379/0"
# Local development only - never set in a deployment:
# ENVIRONMENT="development"
# AUTH_DEV_BYPASS="true"
```
See [`docs/DEPLOYMENT.md`](./docs/DEPLOYMENT.md#environment-variables) for the full list of variables.

**Frontend Environment (`dashboard/.env.local`):**
```bash
NEXT_PUBLIC_SUPABASE_URL="https://your-project.supabase.co"
NEXT_PUBLIC_SUPABASE_ANON_KEY="your_supabase_anon_key"
NEXT_PUBLIC_BACKEND_URL="http://localhost:8000"
```

### 3. Run Backend (FastAPI)
Backend dependencies are managed with [uv](https://docs.astral.sh/uv/) (`backend/pyproject.toml` + `backend/uv.lock`):
```bash
cd backend
uv sync                      # creates backend/.venv from uv.lock (incl. test deps)
uv run uvicorn main:app --reload --port 8000
```

Agent runs are executed by a Celery worker. Without one, runs stay `queued`:
```bash
cd backend
uv run celery -A celery_app worker -Q agent_runs,maintenance,github_sync -l info
```

No `requirements.txt` is committed; run `bash scripts/export-backend-requirements.sh` if a tool needs one. See [CONTRIBUTING.md](./CONTRIBUTING.md#managing-backend-dependencies) for adding or upgrading dependencies.

Alternatively, run the whole backend stack (API, worker, beat, Redis) with Docker:
```bash
docker compose up --build   # http://localhost:8000
```

### 4. Run Frontend (Next.js)
```bash
cd dashboard
npm ci
npm run dev
```
Open `http://localhost:3000` in your browser.

---

## Deployment

Read [`docs/DEPLOYMENT.md`](./docs/DEPLOYMENT.md) before deploying. It covers the topology, every environment variable, and the security checklist. The API and the agent worker **must share the repository workspace directory**.

* **Frontend (Vercel):** Connect the repository to [Vercel](https://vercel.com) with the root directory set to `dashboard/`. It uses [`dashboard/vercel.json`](./dashboard/vercel.json) for the static export and security headers.
* **Backend and workers (Render):** The [`render.yaml`](./render.yaml) blueprint builds the Docker image. It runs the API and the `agent_runs` worker together in one single-instance web service, plus a maintenance/beat worker and Redis. Render disks cannot be shared between services, so splitting them would break the IDE routes.
* **Docker Compose:** `docker compose up --build` starts the API, worker, beat and Redis, with a shared workspace volume.
* **Container image (GHCR):** Published from `main` and release tags (linux/amd64). It runs as a non-root user with `ENVIRONMENT=production`:
  ```bash
  docker pull ghcr.io/pxa-labs/automaintainer:latest
  ```

Never set `AUTH_DEV_BYPASS` in a deployment. Leave `ENABLE_TERMINAL` unset unless you have read the terminal section of the deployment guide.

---

## Security

- **Merging:** keep `MERGE_POLICY` unset or set to `human`. The agents then open and review PRs but never merge them. `auto` merges on an LLM approval alone, without running any tests; do not use it on repositories you care about. Enable branch protection with required checks in any case.
- **Terminal:** `/api/terminal/ws` gives a shell on the API host. It is off unless `ENABLE_TERMINAL=true`, and then only for the user ids in `TERMINAL_ADMIN_USER_IDS`. The shell runs as the server's OS user, so it can read every tenant's clone and the server's files. Leave it off on any shared or hosted deployment.
- **Development bypass:** never set `AUTH_DEV_BYPASS` in a deployment. It is refused when `ENVIRONMENT=production`.

See [SECURITY.md](./SECURITY.md) for how to report a vulnerability and for a summary of the security controls. See the [deployment security checklist](./docs/DEPLOYMENT.md#security-checklist) before deploying.

---

## Architecture
[ARCHITECTURE.md](./ARCHITECTURE.md) describes the components, the run lifecycle, the agent graph, the data model, authentication, and the security limits of each part.

---

## Contributing
Want to add a new Agent or improve the dashboard? Check out our [Contributing Guidelines](./CONTRIBUTING.md).

---

## Release & Changelog
AutoMaintainer maintains a living changelog and automated release notes adhering to [Keep a Changelog](https://keepachangelog.com/en/1.0.0/) and [Conventional Commits](https://www.conventionalcommits.org/). Review all historical and unreleased updates in our [CHANGELOG.md](./CHANGELOG.md).

---

## License
This project is licensed under the [MIT License](./LICENSE).
