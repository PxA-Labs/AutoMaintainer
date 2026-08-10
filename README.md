---
title: AutoMaintainer
emoji: 🚀
colorFrom: blue
colorTo: indigo
sdk: docker
pinned: false
---

<div align="center">
  
# AutoMaintainer
**An Always-On Autonomous AI Software Engineering Team**

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](https://opensource.org/licenses/MIT)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/release/python-3100/)
[![Next.js](https://img.shields.io/badge/Next.js-14-black.svg)](https://nextjs.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.100+-green.svg)](https://fastapi.tiangolo.com/)
[![Hugging Face Spaces](https://img.shields.io/badge/%F0%9F%A4%97%20Hugging%20Face-Spaces-yellow)](https://huggingface.co/spaces/purvansh01/AutoMaintainer)

</div>

AutoMaintainer is a revolutionary proof-of-concept that demonstrates an entirely autonomous, multi-agent software engineering team working natively inside your GitHub repositories.

Built with **LangGraph**, **FastAPI**, **Next.js**, and powered by **Llama 3 (via Groq)**, this system doesn't just print code to a terminal—it brainstorms ideas, creates real GitHub Issues, writes code, submits Pull Requests, reviews the PRs, fixes its own bugs, and merges the code into your `main` branch.

> [!IMPORTANT]
> **Supabase Real-time Architecture Required!**
> This repository was recently upgraded from a legacy WebSocket architecture to a high-performance **Supabase Real-time Pub/Sub** ecosystem. For this early access / open-source version, you **must** create a free Supabase project and provide the URL and API Keys in your `.env` files for the UI to receive logs from the AI agents. See the [Database & Environment Setup](#2-database--environment-setup) section below for exact instructions!
> 
> *(Note: In the upcoming official ecosystem launch, this will be handled automatically via a central cloud and users will simply run `automaintainer login` without needing to configure their own database!)*

---

## Features
- **5-Agent Hierarchy**: Tasks are distributed across specialized agents (Architect, Visionary, Reviewer, Implementer, Maintainer).
- **Native GitHub Integration**: Agents communicate through real GitHub Issues, PR Comments, and Git Branches.
- **Zero-Server Code Intelligence**: Powered by **GitNexus MCP**, allowing agents to semantically navigate your repository and build Code Graphs without sending your code to a third-party server.
- **Web IDE & Interactive Terminal**: A fully integrated, VS Code-style Web IDE in the browser featuring an interactive PTY terminal connecting directly to the backend.
- **Self-Correcting Iteration Loop**: If the Maintainer AI rejects a PR, the Implementer AI reads the feedback and pushes a new commit to fix the bug!
- **Real-time Observability UI**: A sleek, dark-mode React dashboard connected via WebSockets allows you to monitor the AI Crew as they work in real-time.
- **Blazing Fast**: Powered by Groq's LPU inference, the entire cycle from Architecture to Merged PR can happen in under 20 seconds.
- **Cloud Ready**: Natively compatible with Hugging Face Spaces Docker deployments.

---

## Quick Start

### Option 1: Run via Docker (Recommended)
The fastest way to get started is by pulling the pre-built Docker image from the GitHub Container Registry. This includes both the Next.js frontend and FastAPI backend in a single container.

```bash
docker run -p 7860:7860 \
  -e GROQ_API_KEY="your_groq_api_key_here" \
  -e GITHUB_TOKEN="your_github_token_here" \
  ghcr.io/pxa-labs/automaintainer:latest
```
Open your browser to `http://localhost:7860`.

---

### Option 2: Manual Setup

**1. Prerequisites**
- [Node.js](https://nodejs.org/en/) (v18+)
- [Python](https://www.python.org/) (3.10+)
- A [Groq API Key](https://console.groq.com/keys)
- A [GitHub Personal Access Token](https://github.com/settings/tokens) (with `repo` permissions)
- A [Supabase Project](https://supabase.com/) (Free Tier is fine)

### 2. Database & Environment Setup
Clone the repository:
```bash
git clone https://github.com/PxA-Labs/AutoMaintainer.git
cd AutoMaintainer
```

**Supabase Setup:**
Because AutoMaintainer now uses a high-performance Supabase Realtime architecture to stream logs to the UI:
1. Create a new [Supabase Project](https://database.new)
2. Go to your Supabase SQL Editor and run the provided `supabase_schema.sql` file located in the root of this repository. This creates the required `runs` and `logs` tables.
3. Grab your API keys from **Project Settings -> API**.

Create a `.env` file in the `backend/` directory:
```bash
GROQ_API_KEY=your_groq_api_key_here
GITHUB_TOKEN=your_github_token_here
SUPABASE_URL=https://your-project.supabase.co
SUPABASE_SERVICE_KEY=your_secret_service_role_key
```

Create a `.env.local` file in the `dashboard/` directory:
```bash
NEXT_PUBLIC_SUPABASE_URL=https://your-project.supabase.co
NEXT_PUBLIC_SUPABASE_ANON_KEY=your_public_anon_key
NEXT_PUBLIC_BACKEND_URL=http://localhost:8000
```

### 3. Run the Backend (FastAPI + LangGraph)
Open a terminal and navigate to the backend directory:
```bash
cd backend
pip install -r requirements.txt
fastapi dev main.py
```

### 4. Run the Frontend (Next.js)
Open a new terminal and navigate to the dashboard directory:
```bash
cd dashboard
npm install
npm run dev
```
*Note: In local development, the Next.js frontend runs on port 3000 and automatically proxies API traffic to the FastAPI backend running on port 8000. You can override this using the `NEXT_PUBLIC_BACKEND_URL` environment variable.*

---

## Usage

1. Open `http://localhost:3000` in your browser.
2. In the sidebar under **Configuration**, click on the **Target Repository** and enter a repository you own (e.g., `your-username/your-repo`).
   - *Note: Your GitHub Token must have push access to this repository!*
3. Click **Start Agents**.
4. Watch the dashboard UI light up as the Architect scans your repo. Open your target repository on GitHub in another tab to watch the Issues and Pull Requests appear in real-time!

---

## Architecture
Curious how it works under the hood? Read our [Architecture Documentation](./ARCHITECTURE.md) to see how the 5-agent LangGraph topology operates.

---

## Contributing
Want to add a new Agent or improve the dashboard? Check out our [Contributing Guidelines](./CONTRIBUTING.md).

---

## License
This project is licensed under the [MIT License](./LICENSE).
