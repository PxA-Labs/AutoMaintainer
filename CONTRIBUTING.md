# Contributing to AutoMaintainer

Thank you for your interest in contributing to AutoMaintainer! AutoMaintainer is an autonomous, always-on AI software engineering team and platform built with LangGraph, FastAPI, and Next.js.

To ensure high code quality, security, and architectural consistency across the project, all contributors must follow these guidelines.

---

## 1. Code of Conduct

By participating in this project, you agree to abide by our [Code of Conduct](./CODE_OF_CONDUCT.md). Please report any violations or concerns to `maintainers@pxalabs.com`.

---

## 2. Getting Started & Development Setup

### Prerequisites
- **Python:** 3.11 or 3.12
- **Node.js:** 18.x or 20.x
- **Git:** 2.30+
- **Groq API Key:** (for running local agent execution tests)

### Local Environment Setup

1. **Fork and clone** the repository:
   ```bash
   git clone https://github.com/<your-username>/AutoMaintainer.git
   cd AutoMaintainer
   ```

2. **Set up Python Backend** with [uv](https://docs.astral.sh/uv/getting-started/installation/):
   ```bash
   cd backend
   uv sync      # creates backend/.venv from uv.lock, including the dev (test) group
   cd ..
   ```
   Run backend commands through `uv run` (for example `uv run pytest`), or
   activate `backend/.venv` yourself. See
   [Managing backend dependencies](#managing-backend-dependencies).

3. **Set up Next.js Dashboard:**
   ```bash
   cd dashboard
   npm install
   cd ..
   ```

4. **Configure Environment Variables:**
   Copy `.env.example` (or configure `backend/.env` and `dashboard/.env.local`):
   ```ini
   # backend/.env
   GROQ_API_KEY="gsk_your_key"
   GITHUB_TOKEN="ghp_your_token"
   SUPABASE_URL="https://your-project.supabase.co"
   SUPABASE_SERVICE_KEY="your_service_key"
   ```

5. **Set up the database:** apply `supabase/migrations` to your Supabase
   project (`supabase db push`, or
   `DATABASE_URL=... uv run --project backend python scripts/db-migrate up`).
   To change the schema, add a new migration. Do not edit
   `supabase_schema.sql`, which is generated. See
   [docs/DEPLOYMENT.md, "Changing the schema"](docs/DEPLOYMENT.md#changing-the-schema).

---

## 3. Contribution Workflow

### Step 1: Pick or Propose an Issue
- Find something to work on as described in [Finding work](#finding-work).
- For significant changes or new features, please open an issue using the appropriate issue template before starting work.
- Comment on the issue to request assignment.

### Finding work
[GitHub Issues](https://github.com/PxA-Labs/AutoMaintainer/issues) is the only
backlog. The repository has no second, hand-maintained issue list. A file
like that drifts out of date as soon as an issue is closed on GitHub.

- **Epics** are large initiatives with the
  [`epic`](https://github.com/PxA-Labs/AutoMaintainer/issues?q=is%3Aissue+is%3Aopen+label%3Aepic)
  label. Each epic body has a scope checklist. New issues should link to the
  epic they belong to.
- **Area labels**: `backend`, `frontend`, `security`, `testing`,
  `architecture`, `documentation`, `ci/cd`, `docker`.
- **Priority labels**: `critical`, `high-priority`, `medium-priority`,
  `low-priority`.
- **Starter issues**:
  [`good first issue`](https://github.com/PxA-Labs/AutoMaintainer/issues?q=is%3Aissue+is%3Aopen+label%3A%22good+first+issue%22)
  and
  [`help wanted`](https://github.com/PxA-Labs/AutoMaintainer/issues?q=is%3Aissue+is%3Aopen+label%3A%22help+wanted%22).
- **Workflow labels** set by maintainers: `status: needs triage`,
  `status: under review`, `status: on-hold`, `blocked`, `needs-discussion`.

To keep the backlog in one place, do not add issue lists to markdown files.
Link to a label query instead.

### Step 2: Create a Feature Branch
Create a branch named according to the change type:
- `feat/issue-number-short-description`
- `fix/issue-number-short-description`
- `docs/short-description`
- `chore/short-description`

Example:
```bash
git checkout -b feat/183-pydantic-config-schema
```

### Step 3: Implement & Test
- Keep PRs focused, bite-sized, and single-purpose.
- Ensure all new features include unit tests under `backend/test_*.py`.
- Run tests locally before opening a PR:
  ```bash
  # Run backend unit tests
  cd backend && uv run pytest -v && cd ..

  # Format code with Black
  uvx black backend/

  # Check code style with Flake8
  uvx flake8 backend/ --max-line-length=120 --exclude=backend/.venv,backend/venv

  # Dashboard: lint (incl. jsx-a11y), unit tests, build, end-to-end smoke tests
  cd dashboard && npm run lint && npm test && npm run build && npm run test:e2e && cd ..
  ```

### Managing backend dependencies
The backend's dependencies are declared in `backend/pyproject.toml` and pinned
for every platform and Python version in `backend/uv.lock`. Commit both files
together; CI runs `uv lock --check` and fails if the lock does not match
`pyproject.toml`. No `requirements.txt` is committed.

Run these from `backend/`:
```bash
uv add <package>                     # add a runtime dependency
uv add --group dev <package>         # add a test/dev-only dependency
uv remove <package>                  # remove a dependency
uv lock --upgrade-package <package>  # upgrade one package within its constraints
uv lock --upgrade                    # upgrade everything (review the diff carefully)
uv sync                              # apply the lock to backend/.venv
```
If a tool needs a pip requirements file, export one on demand from the
repository root (it is not committed):
```bash
bash scripts/export-backend-requirements.sh requirements.txt
```
Dependabot opens `uv` update PRs that change `pyproject.toml`/`uv.lock`
directly; they need no extra steps.

---

## 4. Conventional Commits & PR Title Standards

AutoMaintainer strictly enforces [Conventional Commits v1.0.0](https://www.conventionalcommits.org/). Our CI pipeline validates all PR titles automatically.

PR titles must follow the format:
```text
<type>(<scope>): <short description>
```

### Allowed Types:
- `feat`: A new feature or capability
- `fix`: A bug fix or defect resolution
- `docs`: Documentation, blueprints, or README updates
- `refactor`: Code changes that neither fix a bug nor add a feature
- `perf`: Performance optimizations
- `test`: Adding or correcting tests
- `ci`: Changes to CI/CD workflows and automation scripts
- `chore`: Routine maintenance, dependencies, or tooling updates
- `security`: Security patches and vulnerability fixes

### Examples:
- `feat(engine): implement instantiable AutoMaintainerEngine factory`
- `fix(agents): handle git clone subprocess exit codes in architect_node`
- `docs(readme): add detailed self-hosted Supabase setup instructions`
- `ci(workflows): pin commit SHAs for OpenSSF Scorecard compliance`

---

## 5. AI-Assisted Contribution Policy

We welcome the use of AI coding assistants (e.g. Gemini, ChatGPT, Claude, Copilot) to help write code, generate tests, and craft documentation. However, **contributors are 100% accountable for the integrity of their submissions**.

### Strict Requirements:
1. **Manual Verification:** You must manually inspect, understand, and locally test every line of code generated by an AI assistant before submitting a PR.
2. **Zero Unverified Hallucinations:** Submissions containing hallucinated imports, invalid mock assertions, duplicated exception blocks, or broken syntax will be flagged with `ai-hallucinated` / `needs-rework` and rejected.
3. **Checklist Confirmation:** Every PR must check the integrity confirmation box in the pull request template.

---

## 6. Submitting a Pull Request

1. Push your branch to your fork:
   ```bash
   git push origin feat/183-pydantic-config-schema
   ```
2. Open a Pull Request targeting the `main` branch of `PxA-Labs/AutoMaintainer`.
3. Fill out the [Pull Request Template](.github/pull_request_template.md) completely, referencing the related issue (`Closes #183`).
4. Ensure all CI/CD checks (CodeQL, Gitleaks, Python Test Matrix, Lint, Vercel Build) pass green.
5. Address maintainer feedback promptly. Once approved, maintainers will merge your contribution.

Thank you for helping build the future of autonomous AI software engineering!
