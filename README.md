<div align="center">
  
# AutoMaintainer
**An Always-On Autonomous AI Software Engineering Team**

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](https://opensource.org/licenses/MIT)
[![OpenSSF Scorecard](https://api.securityscorecards.dev/projects/github.com/PxA-Labs/AutoMaintainer/badge)](https://securityscorecards.dev/viewer/?uri=github.com/PxA-Labs/AutoMaintainer)
[![CI Backend](https://github.com/PxA-Labs/AutoMaintainer/actions/workflows/ci-backend.yml/badge.svg)](https://github.com/PxA-Labs/AutoMaintainer/actions/workflows/ci-backend.yml)
[![CodeQL](https://github.com/PxA-Labs/AutoMaintainer/actions/workflows/codeql-analysis.yml/badge.svg)](https://github.com/PxA-Labs/AutoMaintainer/actions/workflows/codeql-analysis.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/release/python-3110/)
[![Next.js](https://img.shields.io/badge/Next.js-16-black.svg)](https://nextjs.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.100+-green.svg)](https://fastapi.tiangolo.com/)
[![Vercel](https://img.shields.io/badge/Vercel-Deployed-black?logo=vercel)](https://vercel.com/new/clone?repository-url=https%3A%2F%2Fgithub.com%2FPxA-Labs%2FAutoMaintainer&root-directory=dashboard)

<p align="center">
  <a href="https://vercel.com/new/clone?repository-url=https%3A%2F%2Fgithub.com%2FPxA-Labs%2FAutoMaintainer&root-directory=dashboard">
    <img src="https://vercel.com/button" alt="Deploy with Vercel"/>
  </a>
</p>

</div>

AutoMaintainer is an autonomous, multi-agent AI software engineering platform operating natively inside your GitHub repositories.

Built with **LangGraph**, **FastAPI**, **Next.js**, and powered by **Llama 3 (via Groq)**, the system coordinates specialized AI agents to discover issues, design architecture, implement code changes, open Pull Requests, review diffs, self-correct bugs, and merge verified code.

---

> [!WARNING]
> ### 🚧 Active Architecture Migration Notice (System Unstable)
> **AutoMaintainer is currently undergoing a massive architectural transformation from a local prototype to an enterprise-grade Web-First SaaS Platform.**
>
> During this major migration phase:
> * **The platform is currently unstable and active workflows may be temporarily non-operational or experience breaking changes.**
> * Core systems being actively overhauled include:
>   - **Multi-Tenant SaaS Control Plane:** Migrating from single-user local state to organization-scoped data isolation and Row Level Security (RLS).
>   - **Durable Task Queue:** Replacing in-memory asyncio task execution with Celery + Redis distributed workers and retry mechanisms.
>   - **GitHub App Authentication:** Transitioning from personal access tokens (PAT) to GitHub App installation tokens with automated webhook dispatching.
>   - **Ephemeral Sandbox Architecture:** Decoupling execution runners for containerized code execution.
>   - **Pro Monaco WebIDE:** Integrating full diff inspection and inline AI assistance.
>
> Please follow our [GitHub Discussions](https://github.com/PxA-Labs/AutoMaintainer/discussions) and [Pinned Epics](https://github.com/PxA-Labs/AutoMaintainer/issues) for real-time progress updates.

---

> [!IMPORTANT]
> ### 🗄️ Critical Supabase & Database Configuration Note
> AutoMaintainer relies on **Supabase** (PostgreSQL + Realtime Pub/Sub + Auth) for telemetry, run orchestration, and live agent log streaming.
>
> 1. **Public/Demo Database Status:** If the free-tier demo Supabase instance is paused or unreachable, historical run tracking and real-time streaming to the Web UI will be disabled. The UI will show a warning banner indicating that persistence is offline.
> 2. **Self-Hosted / Developer Database Setup:** If you are running or contributing to AutoMaintainer, you **must provision your own Supabase project**:
>    * Create a project at [supabase.com](https://supabase.com).
>    * Run the complete schema migration script [`supabase_schema.sql`](./supabase_schema.sql) in your **Supabase SQL Editor** to create the required tables (`organizations`, `users`, `repositories`, `runs`, `logs`, `usage_events`, `ide_sessions`).
>    * Supply your own credentials in `.env` (backend) and `.env.local` (dashboard).

---

## Features
- **5-Agent Hierarchy**: Tasks are distributed across specialized agents (Architect, Visionary, Reviewer, Implementer, Maintainer).
- **Native GitHub Integration**: Agents communicate through real GitHub Issues, PR Comments, and Git Branches.
- **Zero-Server Code Intelligence**: Powered by **GitNexus MCP**, allowing agents to semantically navigate your repository and build Code Graphs without sending your code to a third-party server.
- **Web IDE & Interactive Terminal**: A fully integrated, VS Code-style Web IDE in the browser featuring an interactive PTY terminal connecting directly to the backend.
- **Self-Correcting Iteration Loop**: If the Maintainer AI rejects a PR, the Implementer AI reads the feedback and pushes a new commit to fix the bug!
- **Real-time Observability UI**: A sleek, dark-mode React dashboard connected via WebSockets/SSE allows you to monitor the AI Crew as they work in real-time.
- **Blazing Fast**: Powered by Groq's LPU inference, the entire cycle from Architecture to Merged PR can happen in under 20 seconds.
- **Cloud Ready**: Production-ready deployment configurations for Vercel, Render, and Docker.

---

## Development & Local Setup

### 1. Prerequisites
- [Node.js](https://nodejs.org/en/) (v20+)
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

**Database Migration:**
1. Open your [Supabase SQL Editor](https://database.new).
2. Execute the entire contents of [`supabase_schema.sql`](./supabase_schema.sql).

**Backend Environment (`backend/.env`):**
```bash
GROQ_API_KEY="your_groq_api_key_here"
GITHUB_TOKEN="your_github_token_here"
SUPABASE_URL="https://your-project.supabase.co"
SUPABASE_SERVICE_KEY="your_supabase_service_role_key"
REDIS_URL="redis://localhost:6379/0"
```

**Frontend Environment (`dashboard/.env.local`):**
```bash
NEXT_PUBLIC_SUPABASE_URL="https://your-project.supabase.co"
NEXT_PUBLIC_SUPABASE_ANON_KEY="your_supabase_anon_key"
NEXT_PUBLIC_BACKEND_URL="http://localhost:8000"
```

### 3. Run Backend (FastAPI)
```bash
cd backend
pip install -r requirements.txt
fastapi dev main.py
```

### 4. Run Frontend (Next.js)
```bash
cd dashboard
npm ci
npm run dev
```
Open `http://localhost:3000` in your browser.

---

## Deployment (Production SaaS)

AutoMaintainer is configured for distributed cloud deployment:

* **Frontend (Vercel):** Connect the repository to [Vercel](https://vercel.com) with root directory set to `dashboard/`. Uses [`dashboard/vercel.json`](./dashboard/vercel.json) for static export and security headers.
* **Backend & Workers (Render):** Deploy using the included [`render.yaml`](./render.yaml) blueprint to launch the FastAPI control plane, Redis broker, and Celery background workers.
* **Containerized Deployment (Docker / GHCR):** Pull the pre-built multi-arch image:
  ```bash
  docker pull ghcr.io/pxa-labs/automaintainer:latest
  ```

---

## Architecture
Curious how it works under the hood? Read our [Architecture Documentation](./ARCHITECTURE.md) to see how the 5-agent LangGraph topology operates.

---

## Contributing
Want to add a new Agent or improve the dashboard? Check out our [Contributing Guidelines](./CONTRIBUTING.md).

---

## Release & Changelog
AutoMaintainer maintains a living changelog and automated release notes adhering to [Keep a Changelog](https://keepachangelog.com/en/1.0.0/) and [Conventional Commits](https://www.conventionalcommits.org/). Review all historical and unreleased updates in our [CHANGELOG.md](./CHANGELOG.md).

---

## License
This project is licensed under the [MIT License](./LICENSE).
