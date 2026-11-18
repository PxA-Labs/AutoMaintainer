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

The pipeline is a LangGraph graph. It holds all authority: run state,
cancellation, the retry loop, the trust gate, the merge policy and every
GitHub write. The same engine runs behind the FastAPI server and Celery
workers, and on its own as the `automaintainer` Python package
(`AutoMaintainerEngine`). Optional parts, such as the ADK Reviewer, code
retrieval, the sandbox backends and Cloud Trace and Cloud Monitoring export,
are off by default.

The model is **Llama 3.3 70B on Groq**. On the main (GitNexus tool) path, a
failed 70B call falls back to **Llama 3.1 8B** without notice, so reviews and
code may come from the smaller model. See [ARCHITECTURE.md](./ARCHITECTURE.md)
for how the pieces fit together.

---

> [!IMPORTANT]
> ### Supabase is required
> AutoMaintainer stores organizations, runs and logs in **Supabase** (Postgres,
> Auth and Realtime). Provision your own project:
> * Create a project at [supabase.com](https://supabase.com).
> * Apply the migrations in [`supabase/migrations`](./supabase/migrations)
>   with `supabase db push` or `uv run --project backend python scripts/db-migrate up`, or paste the
>   generated [`supabase_schema.sql`](./supabase_schema.sql) into the
>   **Supabase SQL Editor**. They create `organizations`,
>   `organization_members`, `github_installations`, `repositories`,
>   `repository_merge_policy_changes`, `runs`, `logs`, `usage_events`,
>   `webhook_events`, `ide_sessions`, `verification_results`,
>   `merge_decisions` and `code_chunks` (which needs the `vector`
>   extension), with RLS policies. All of them can be re-run, and upgrades
>   keep your data ([Database setup and upgrades](./docs/DEPLOYMENT.md#database)).
> * Put your credentials in `backend/.env` and `dashboard/.env.local`.
>
> If Supabase is unreachable or paused, the dashboard shows a warning banner,
> and run history and live logs are unavailable.

---

## Features
Each item names the code it is based on. Items marked *opt-in* are off
unless you turn them on.

**Pipeline**
- **Five-agent LangGraph pipeline** (`backend/automaintainer/_runtime/agents.py`): Architect, Visionary, Reviewer/PM, Implementer and Maintainer. If the Maintainer rejects a change, the Implementer revises it, up to three Implementer attempts in total.
- **Two modes**: autonomous (the Architect proposes the work) or targeted (`target_issue`: work on an existing issue). The issue must name the file to change. A targeted run starts from the dashboard or the API, or, when `AUTOMAINTAINER_TRIGGER_LABEL` is set, when that label is added to an open issue (`backend/webhook_processor.py`).
- **Works through GitHub**: issues, issue comments, branches, commits, pull requests and review comments, using the repository's GitHub App installation token (`backend/automaintainer/_runtime/github_app.py`).
- **Human-gated merging**: `MERGE_POLICY=human` (the default) never merges. `ci` merges only when the repository's own tests pass (the `automaintainer/verification` status; see [docs/VERIFICATION.md](./docs/VERIFICATION.md)) and every other check on the PR head passes; no checks at all counts as failing. Under `ci` and `auto`, a merge also needs a trusted trigger: every GitHub user who caused the run, and the author of a targeted issue, must have write access to the repository (`backend/automaintainer/_runtime/trust.py`). See the merge-policy table in [ARCHITECTURE.md](./ARCHITECTURE.md#merge-policy).
- **Structured decisions**: the PM and Maintainer return JSON validated against a schema; anything that does not validate after one retry counts as a rejection.
- **Durable runs**: a Celery worker runs each job, with a per-repository Redis lock, heartbeats, stale-run cleanup and cancellation (`backend/tasks.py`). A run that a crashed worker delivers again resumes from `runs.checkpoint`, without duplicate issues, branches or PRs.
- **Prompt-injection hardening**: repository text, issue text and retrieved code reach the model inside delimited untrusted-data blocks. Red-team fixtures in `backend/tests/redteam/` test this offline.
- **Code context**: an AST map of the clone (tree-sitter) and GitNexus tools over MCP. GitNexus runs locally. Prompts, including repository content, are sent to Groq.

**Engine and extensions**
- **Python SDK** (`backend/automaintainer/`): `AutoMaintainerEngine` runs the pipeline in-process with per-engine configuration, isolated concurrent runs and cancellation. The distribution name is `automaintainer`; the extras are `server` (FastAPI, Celery, OpenTelemetry), `adk` and `rag`. See [Python SDK](#python-sdk).
- **ADK Reviewer** (*opt-in*, `backend/automaintainer/_specialists/`): a read-only, tool-using reviewer built with Google's Agent Development Kit, selected with `REVIEWER_BACKEND=adk` and the `adk` extra. The default is `langgraph`, the single-prompt review. Design: [docs/design/adk-specialist-agents.md](./docs/design/adk-specialist-agents.md).
- **Code retrieval** (*opt-in*, [docs/RAG.md](./docs/RAG.md)): function-level chunks of the target repository in `code_chunks` on pgvector, with org-scoped RLS. With `RAG_RETRIEVAL_ENABLED`, the PM prompt gets the nearest chunks and the ADK Reviewer's `search_code` can use the index. Indexing (`RAG_INDEXING_ENABLED`) embeds the code with Vertex AI (the `rag` extra), so repository code is sent to Google Cloud.
- **Sandboxed verification** (*opt-in*, [docs/VERIFICATION.md](./docs/VERIFICATION.md)): the target repository's tests run through a sandbox backend selected with `SANDBOX_BACKEND`. Two backends ship: Docker (`runners/docker_sandbox.py`) and Cloud Run Jobs (`runners/cloud_run_job_sandbox.py`). Without a backend, verification refuses to run on the host unless `VERIFY_ALLOW_LOCAL=true`, so `ci` does not merge.

**Operations**
- **Dashboard** (`dashboard/`): start and stop runs, live logs through Supabase Realtime, run history and an org admin page.
- **Web IDE**: a Monaco editor over the repository clone, inline AI assist (rate-limited per user and per organization) and a "propose changes" flow that opens a PR.
- **Opt-in admin terminal**: a PTY over WebSocket, off unless `ENABLE_TERMINAL=true`, and limited to `TERMINAL_ADMIN_USER_IDS`.
- **Evaluation** ([docs/EVALS.md](./docs/EVALS.md)): an Implementer harness (`backend/evals/`, 30 synthetic tasks with hidden tests) and an end-to-end evaluation of the whole engine (`backend/evals/e2e/`, 24 tasks from real upstream files, plus 27 Reviewer cases). Offline oracle, no-op and replay modes run in CI; live runs against Groq are manual. No live results are published yet.
- **Observability** ([docs/OBSERVABILITY.md](./docs/OBSERVABILITY.md)): OpenTelemetry traces and metrics with bounded labels, including run outcomes, run duration, LLM tokens and an estimated LLM cost. Export to Cloud Trace (`OTEL_CLOUD_TRACE_ENABLED`) and Cloud Monitoring (`OTEL_CLOUD_MONITORING_ENABLED`) is *opt-in*.
- **Deployment options**: Docker image (non-root), Docker Compose, a Render blueprint, a static dashboard export for Vercel, and Terraform for Google Cloud. See [Deployment](#deployment).

No end-to-end latency or success-rate figures are claimed. None have been
measured on live models.

---

## Quickstart

- **Run it**: follow [docs/DEPLOYMENT.md](./docs/DEPLOYMENT.md). `docker compose up --build` starts the API, a worker, beat and Redis. You still need Supabase, a Groq key and GitHub credentials.
- **Develop it**: follow the setup below and [CONTRIBUTING.md](./CONTRIBUTING.md).

---

## Development & Local Setup

### 1. Prerequisites
- [Node.js](https://nodejs.org/en/) (v22.18+; the gitnexus CLI requires `^22.18 || >=24.11`)
- [Python](https://www.python.org/) (3.11+)
- [uv](https://docs.astral.sh/uv/) (backend dependencies; CI uses 0.13.8)
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
uv sync --extra server       # creates backend/.venv from uv.lock (incl. the dev group: pytest, black, flake8)
# optional: --extra adk (google-adk + litellm), --extra rag (google-genai, Vertex AI embeddings)
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

### Python SDK
The engine also installs on its own as the `automaintainer` package, without Celery or the server code (`main`, `tasks`). The base install covers `AutoMaintainerEngine`. The extras add optional parts: `server` (FastAPI, Celery and OpenTelemetry, for example `pip install "./backend[server]"`), `adk` (the ADK Reviewer) and `rag` (Vertex AI embeddings for code retrieval). Publishing to PyPI is off until the owner enables it (`.github/workflows/publish-sdk.yml`), so build the wheel from the repository:
```bash
cd backend && uv build        # dist/automaintainer-<version>-py3-none-any.whl (+ the sdist)
```
```python
import asyncio
from automaintainer import AutoMaintainerConfig, AutoMaintainerEngine

async def main():
    config = AutoMaintainerConfig(github_token="ghp_...", groq_api_keys=["gsk_..."])
    async with AutoMaintainerEngine(config) as engine:
        result = await engine.run("owner/repo", target_issue=42)
        print(result.status, result.pr_url)

asyncio.run(main())
```
See [ARCHITECTURE.md](./ARCHITECTURE.md#engine) for what an engine isolates.

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
* **Google Cloud (Terraform):** [`infra/terraform`](./infra/terraform/README.md) describes the API on Cloud Run, the workers on a Compute Engine VM, Secret Manager, Artifact Registry, a billing budget, the Cloud Run Jobs verification sandbox and optional monitoring. `.github/workflows/deploy-gcp.yml` deploys through Workload Identity Federation, with no stored key. The owner steps are in [docs/DEPLOYMENT.md, Option D](./docs/DEPLOYMENT.md#option-d-google-cloud-terraform). The configuration has not yet been applied to a live project.

Never set `AUTH_DEV_BYPASS` in a deployment. Leave `ENABLE_TERMINAL` unset unless you have read the terminal section of the deployment guide.

---

## Security

- **Merging:** keep `MERGE_POLICY` unset or set to `human`. The agents then open and review PRs but never merge them. `auto` merges on the Maintainer's approval and a trusted trigger alone, without running any tests; do not use it on repositories you care about. Enable branch protection with required checks in any case.
- **Verification:** the target repository's tests are untrusted code. Run them through a sandbox backend, and do not set `VERIFY_ALLOW_LOCAL=true` on a shared host.
- **Terminal:** `/api/terminal/ws` gives a shell on the API host. It is off unless `ENABLE_TERMINAL=true`, and then only for the user ids in `TERMINAL_ADMIN_USER_IDS`. The shell runs as the server's OS user, so it can read every tenant's clone and the server's files. Leave it off on any shared or hosted deployment.
- **Development bypass:** never set `AUTH_DEV_BYPASS` in a deployment. It is refused when `ENVIRONMENT=production`.

See [SECURITY.md](./SECURITY.md) for how to report a vulnerability and for a summary of the security controls. See the [deployment security checklist](./docs/DEPLOYMENT.md#security-checklist) before deploying.

---

## Documentation

- [ARCHITECTURE.md](./ARCHITECTURE.md): components, run lifecycle, agent graph, engine, data model, authentication and security limits.
- [docs/DEPLOYMENT.md](./docs/DEPLOYMENT.md): deployment options, environment variables, database migrations and the security checklist.
- [docs/VERIFICATION.md](./docs/VERIFICATION.md): how agent changes are tested, merge gating and the sandbox backends.
- [docs/EVALS.md](./docs/EVALS.md): the Implementer harness and the end-to-end evaluation.
- [docs/RAG.md](./docs/RAG.md): code retrieval, tenancy, data egress and cost.
- [docs/OBSERVABILITY.md](./docs/OBSERVABILITY.md): the metric catalogue and its label rules.
- [docs/design/adk-specialist-agents.md](./docs/design/adk-specialist-agents.md): design note for ADK specialist agents under the LangGraph engine.
- [infra/terraform/README.md](./infra/terraform/README.md): the Google Cloud Terraform configuration.
- [SECURITY.md](./SECURITY.md): reporting a vulnerability, and a summary of the security controls.
- [CONTRIBUTING.md](./CONTRIBUTING.md): development setup, tests, CI checks and pull request conventions.

---

## Status and roadmap

The evaluation baseline, the Google Cloud deployment, the ADK Reviewer, code
retrieval, and metrics with alerting are implemented in code. Each one is off
by default or needs an owner to deploy it:

| Feature | How it is turned on |
| --- | --- |
| End-to-end evaluation | Offline modes run in CI. Live runs: manual dispatch of `evals.yml` with the suite `e2e` (needs the `GROQ_API_KEY` secret). |
| Google Cloud deployment | `terraform apply` in `infra/terraform`, then `deploy-gcp.yml`. |
| ADK Reviewer | `REVIEWER_BACKEND=adk` with the `adk` extra installed. The default is `langgraph`. |
| Code retrieval | `RAG_INDEXING_ENABLED` and `RAG_RETRIEVAL_ENABLED`, or `rag_enabled` in Terraform. |
| Sandboxed verification | `SANDBOX_BACKEND`. Terraform sets it to `cloud_run_job` on the Google Cloud worker only. |
| Cloud Trace and Cloud Monitoring | `OTEL_CLOUD_TRACE_ENABLED` and `OTEL_CLOUD_MONITORING_ENABLED`, or `cloud_trace_enabled`, `cloud_monitoring_enabled` and `monitoring_app_alerts_enabled` in Terraform. |

What remains is validation that needs credentials or costs money. The owner
checklist is in [#532](https://github.com/PxA-Labs/AutoMaintainer/issues/532):

1. **First deployment** ([#527](https://github.com/PxA-Labs/AutoMaintainer/issues/527)): apply Terraform, add the secret values (including `redis-url`), run the sandbox smoke test, then turn on Cloud Monitoring and the application alerts.
2. **Live evaluation baseline**: the first live end-to-end run against Groq.
3. **ADK spike and Reviewer comparison** ([#525](https://github.com/PxA-Labs/AutoMaintainer/issues/525)): run `evals/spike_adk_groq.py` for a go or no-go on Groq tool calling, then compare the two Reviewer backends over at least three repeats. `REVIEWER_BACKEND` stays `langgraph` unless the false-approval rate does not get worse.
4. **Vertex recall check** ([#526](https://github.com/PxA-Labs/AutoMaintainer/issues/526)): measure recall with Vertex AI embeddings before turning on indexing or retrieval.

---

## Contributing
See [CONTRIBUTING.md](./CONTRIBUTING.md) for the development setup, tests, CI checks and pull request conventions.

---

## Release and changelog
[CHANGELOG.md](./CHANGELOG.md) follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/). It is updated automatically from [Conventional Commits](https://www.conventionalcommits.org/) pull request titles.

---

## License
This project is licensed under the [MIT License](./LICENSE).
