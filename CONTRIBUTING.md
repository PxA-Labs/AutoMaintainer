# Contributing to AutoMaintainer

AutoMaintainer is a multi-agent pipeline that turns repository issues into
reviewed GitHub pull requests. It is built with LangGraph, FastAPI, Celery,
Supabase and Next.js. This guide covers the development setup, tests and
linters, the CI checks, and the conventions a pull request must follow.

## 1. Code of Conduct

By participating in this project, you agree to abide by the
[Code of Conduct](./CODE_OF_CONDUCT.md). Report violations or concerns to
`maintainers@pxalabs.com`.

Report security vulnerabilities privately, as described in
[SECURITY.md](./SECURITY.md), never in a public issue or pull request.

## 2. Development setup

### Prerequisites

- **Python** 3.11 or 3.12 (the versions the backend CI tests).
- **[uv](https://docs.astral.sh/uv/getting-started/installation/)** for the
  backend. CI uses uv 0.13.8, the same version as the Dockerfile.
- **Node.js** 22.18 or later in the 22 line, or 24.11 or later, for the
  dashboard and the gitnexus CLI (gitnexus needs `^22.18 || >=24.11`; CI uses
  Node.js 22.x).
- **Git**.
- **[ripgrep](https://github.com/BurntSushi/ripgrep)** (`rg`), only for the
  ADK Reviewer tests; they skip without it.
- A **Groq API key**, only to run the agents or live evaluations. The test
  suite mocks every model call.

### Backend

1. Fork and clone the repository:
   ```bash
   git clone https://github.com/<your-username>/AutoMaintainer.git
   cd AutoMaintainer
   ```

2. Create the backend environment from `backend/uv.lock`:
   ```bash
   cd backend
   uv sync --extra server
   ```
   `uv sync` installs the `dev` dependency group by default: pytest and its
   plugins, and the exact `black` and `flake8` versions CI runs. The `server`
   extra adds uvicorn, Celery and OpenTelemetry, which the server and most
   tests need. Two more extras are optional:

   | Extra | Adds | Needed for |
   | --- | --- | --- |
   | `adk` | `google-adk`, `litellm` | The ADK Reviewer (`backend/automaintainer/_specialists/`) and its tests |
   | `rag` | `google-genai` | Vertex AI embeddings for code retrieval and the retrieval tests |

   To work on those parts, install everything:
   ```bash
   uv sync --extra server --extra adk --extra rag
   ```
   Run backend commands through `uv run` (for example `uv run pytest`), or
   activate `backend/.venv` yourself.

3. Configure `backend/.env`. See the
   [README](./README.md#2-environment-configuration) for a minimal file and
   [docs/DEPLOYMENT.md](./docs/DEPLOYMENT.md#environment-variables) for every
   variable. Never commit `.env` files or real keys.

4. Set up the database: apply `supabase/migrations` to your Supabase project
   (`supabase db push`, or
   `DATABASE_URL=... uv run --project backend --extra server python scripts/db-migrate up`).
   To change the schema, add a new migration and regenerate
   `supabase_schema.sql`; never edit that file by hand. See
   [docs/DEPLOYMENT.md, "Changing the schema"](./docs/DEPLOYMENT.md#changing-the-schema).

### Dashboard

```bash
cd dashboard
npm ci
```
Configure `dashboard/.env.local` as described in the
[README](./README.md#2-environment-configuration). See
[dashboard/README.md](./dashboard/README.md) for the dashboard's own scripts.

## 3. Running tests and linters

### Backend tests

From `backend/`:
```bash
uv run pytest                     # the full suite, as the CI test matrix runs it
uv run pytest tests/redteam -v    # prompt-injection red-team fixtures only
```
With the `adk` and `rag` extras installed (and ripgrep on `PATH`), run the
tests that otherwise skip:
```bash
uv run pytest tests/specialists tests/concurrency/test_concurrency_adk_reviewer.py tests/rag tests/redteam
```
The row-level security tests in `tests/rls` need a PostgreSQL 16 database
with pgvector and skip without one:
```bash
DATABASE_URL_RLS_TEST=postgresql://postgres@localhost:5432/postgres uv run pytest tests/rls
```
New backend tests go next to the code they cover: `backend/test_*.py` or
`backend/tests/`.

The evaluation harnesses have their own commands and modes; see
[docs/EVALS.md](./docs/EVALS.md).

### Formatting and lint

`black` and `flake8` are exact pins in the `dev` group, so `uv run` uses the
versions CI uses. From `backend/`:
```bash
uv run black .                    # format; CI runs `black --check .`
uv run flake8 . --extend-exclude=.venv,venv --count --select=E9,F63,F7,F82 --show-source --statistics
```
The flake8 selection above is the blocking gate in CI. CI also runs a
non-blocking flake8 pass with `--max-complexity=10 --max-line-length=127`.
The `--extend-exclude` is needed locally because flake8 does not skip
`backend/.venv`; CI keeps its virtual environment outside the source tree
instead.

### Dashboard

From `dashboard/`:
```bash
npm run lint       # ESLint, including the jsx-a11y rules
npm test           # Vitest unit and component tests
npm run build      # static export
npm run test:e2e   # Playwright smoke tests; first run `npx playwright install chromium`
```

### Terraform

If you change `infra/terraform`, run the same checks as CI from that
directory (Terraform 1.9 or later; CI uses 1.17.1):
```bash
terraform fmt -check -recursive
terraform init -backend=false -input=false -lockfile=readonly
terraform validate
```
These need no cloud credentials.

## 4. Managing backend dependencies

The backend's dependencies are declared in `backend/pyproject.toml` and pinned
for every platform and Python version in `backend/uv.lock`. Commit both files
together; CI runs `uv lock --check` and fails if the lock does not match
`pyproject.toml`. No `requirements.txt` is committed.

The base install is the `automaintainer` SDK. A dependency that only the
server code needs (`main.py`, `tasks.py`, `celery_app.py`, `quotas.py`,
`observability.py`) belongs in the `server` extra, so the SDK wheel stays
small; the `sdk-wheel` CI job checks that the wheel imports in a clean
environment without it.

Run these from `backend/`:
```bash
uv add <package>                     # add an engine (SDK) dependency
uv add --optional server <package>   # add a server-only dependency
uv add --group dev <package>         # add a test/dev-only dependency
uv remove <package>                  # remove a dependency
uv lock --upgrade-package <package>  # upgrade one package within its constraints
uv lock --upgrade                    # upgrade everything (review the diff carefully)
uv sync                              # apply the lock to backend/.venv
```
Bump `black` or `flake8` only in a commit that also applies the new style.

If a tool needs a pip requirements file, export one on demand from the
repository root (it is not committed):
```bash
bash scripts/export-backend-requirements.sh requirements.txt
```
Dependabot opens update PRs for uv, npm, GitHub Actions and Docker. The uv
PRs change `pyproject.toml` and `uv.lock` directly and need no extra steps.

## 5. CI checks

Each workflow runs only when the paths it watches change. A pull request is
expected to pass every check that runs on it.

| Workflow | Job | Fails when |
| --- | --- | --- |
| `ci-backend.yml` | Lint & Style Checks | `uv.lock` does not match `pyproject.toml`, `black --check` would reformat a file, or the blocking flake8 selection reports an error |
| | Security & Vulnerability Scan | Bandit reports a medium or higher issue, or pip-audit finds a vulnerable locked package |
| | SDK wheel (clean venv import) | The wheel ships anything but the `automaintainer` package, or importing it loads a server module or fails |
| | Test with the adk and rag extras | A specialist, ADK Reviewer, retrieval or red-team test fails or skips |
| | Test (Ubuntu and Windows, Python 3.11 and 3.12) | Any backend test fails |
| `ci-db.yml` | Contract & RLS tests (PostgreSQL 16) | `supabase_schema.sql` is out of date, a migration fails or does not re-apply cleanly, the OpenAPI or dashboard contract changes, or an RLS test fails or skips |
| `redteam.yml` | Prompt-injection red-team fixtures | A red-team fixture fails |
| `evals.yml` | Harness, baselines & replay; End-to-end (data, oracle, no-op, replay) | An offline evaluation gate is missed (see [docs/EVALS.md](./docs/EVALS.md)) |
| `sandbox.yml` | Docker sandbox (real containers) | The Docker sandbox backend misbehaves against a real Docker daemon |
| `compose-smoke.yml` | docker compose up | The Compose stack does not start or a run does not reach a terminal state |
| `ci-frontend.yml` | `build-and-lint`, `e2e` | Dashboard lint, unit tests, build or Playwright smoke tests fail |
| `docker-publish.yml` | `build-and-push-image` (no push on pull requests) | The Docker image does not build; runs when the Dockerfile or the lock files change |
| `ci-terraform.yml` | fmt, validate | `terraform fmt` or `terraform validate` fails |
| `secret-scanning.yml` | Gitleaks Secret Scanner | Gitleaks finds a secret in the history |
| `codeql-analysis.yml` | Analyze | The analysis fails. Findings for Python and JavaScript/TypeScript appear as code-scanning alerts |
| `security-audit.yml` | Python Bandit & Pip-Audit; Node NPM Audit | Bandit or pip-audit report an issue, or `npm audit` finds a critical vulnerability |
| `lint-pr-title.yml` | Validate Conventional PR Title | The title breaks the rules in [section 6](#6-pull-request-titles) |

`deploy-gcp.yml`, `publish-sdk.yml`, `release.yml`, the image push in
`docker-publish.yml` and the live evaluation jobs run only on `main`, on tags
or on manual dispatch, never on pull requests.

### Gitleaks false positives

Gitleaks scans the full history, so a fake key in a test can fail the
`secret-scanning` job. Fix a false positive in this order:

1. Avoid key-shaped literals in tests and docs where you can.
2. Add an inline `gitleaks:allow` comment to the line, with the reason, for
   example `secret = "gsk_fake_0123"  # gitleaks:allow (fake key)`. In
   Markdown, use `<!-- gitleaks:allow -->`.
3. For a finding in a commit that is already in the history, add its
   fingerprint (`<commit>:<path>:<rule>:<line>`, as Gitleaks prints it) to
   [`.gitleaksignore`](./.gitleaksignore), with a comment that says why it is
   a false positive.

Never allow-list a real secret. If one was committed, rotate it first.

## 6. Pull request titles

The pull request title becomes the changelog entry and, when the pull request
is squash-merged, the commit message on `main`. `lint-pr-title.yml` checks
every title against [Conventional Commits](https://www.conventionalcommits.org/):

```text
<type>(<optional scope>): <subject>
```

- **Types:** `feat`, `fix`, `docs`, `style`, `refactor`, `perf`, `test`,
  `build`, `ci`, `chore`, `revert`, `security`.
- **Scope:** optional, for example `engine`, `rag`, `dashboard`.
- **Subject:** must not start with an uppercase letter. Write it in the
  imperative mood, for example "add", not "Added".

Examples:
- `feat(engine): add a retriever factory to AutoMaintainerEngine`
- `fix(webhooks): answer 401 to bad signatures`
- `docs(readme): describe the Google Cloud deployment`
- `ci(backend): run the adk and rag extra tests`

## 7. Contribution workflow

### Finding work

[GitHub Issues](https://github.com/PxA-Labs/AutoMaintainer/issues) is the only
backlog. Do not add issue lists to Markdown files; link to a label query
instead.

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

For a significant change or a new feature, open an issue with the matching
template before you start, and comment on it to ask for assignment.

### Branches

Name the branch after the change type:
- `feat/<issue-number>-<short-description>`
- `fix/<issue-number>-<short-description>`
- `docs/<short-description>`
- `chore/<short-description>`

### Opening the pull request

1. Keep each pull request focused on one change, with tests for new behaviour.
2. Run the relevant tests and linters from [section 3](#3-running-tests-and-linters).
3. Push the branch to your fork and open a pull request against `main`.
4. Fill in the [pull request template](./.github/pull_request_template.md) and
   reference the issue (`Closes #123`).
5. Make every CI check that runs on the pull request pass, and address review
   feedback. A maintainer merges it once it is approved.

## 8. Documentation

- Write plain, precise English: short sentences, active voice, present tense.
  Check every flag, default, path and command against the code before you
  write it.
- Each topic has one canonical page. Link to it instead of repeating it:

  | Topic | Page |
  | --- | --- |
  | Overview, features, quick setup | [README.md](./README.md) |
  | Components, run lifecycle, data model | [ARCHITECTURE.md](./ARCHITECTURE.md) |
  | Deployment, environment variables, migrations | [docs/DEPLOYMENT.md](./docs/DEPLOYMENT.md) |
  | Verification and sandbox backends | [docs/VERIFICATION.md](./docs/VERIFICATION.md) |
  | Evaluation | [docs/EVALS.md](./docs/EVALS.md) |
  | Code retrieval | [docs/RAG.md](./docs/RAG.md) |
  | Metrics | [docs/OBSERVABILITY.md](./docs/OBSERVABILITY.md) |
  | Google Cloud infrastructure | [infra/terraform/README.md](./infra/terraform/README.md) |
  | Design notes | [`docs/design/`](./docs/design) |

- Update the documentation in the same pull request as the behaviour it
  describes. A new metric, for example, must also be added to
  `docs/OBSERVABILITY.md`, which `backend/test_metric_labels.py` checks.
- Use relative links and make sure each target file and heading exists.
- Do not edit `CHANGELOG.md`; an automated pull request maintains it from
  merged pull request titles.

## 9. AI-assisted contributions

You may use AI coding assistants to help write code, tests and documentation.
You remain fully accountable for what you submit:

1. **Manual verification:** inspect, understand and test every generated line
   locally before you open a pull request.
2. **No unverified output:** submissions with invented imports or APIs,
   invalid mock assertions, duplicated exception blocks or broken syntax are
   labelled `ai-hallucinated` or `needs-rework` and rejected.
3. **Checklist confirmation:** confirm the integrity items in the pull request
   template.
