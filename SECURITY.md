# Security Policy

## Supported versions

AutoMaintainer is pre-1.0 software. Security fixes go to the `main` branch
only. The `v0.9.0-beta` pre-release and older tags do not receive backported
fixes; upgrade to the latest `main` (or the `ghcr.io/pxa-labs/automaintainer:latest`
image built from it).

| Version | Supported |
| ------- | --------- |
| `main` (latest commit) | Yes |
| Any tag or release, including `v0.9.0-beta` | No |

## Reporting a vulnerability

**Do not open a public issue, pull request or discussion for a vulnerability.**

Report it privately through GitHub:

1. Open the repository's [Security tab](https://github.com/PxA-Labs/AutoMaintainer/security).
2. Choose **Report a vulnerability**. This creates a private security advisory
   that only you and the maintainers can see.

If the **Report a vulnerability** button is not shown, private vulnerability
reporting is not enabled for the repository, and the "Security Vulnerability
Reporting" link in the issue chooser will not work either. In that case, start
a [GitHub Discussion](https://github.com/PxA-Labs/AutoMaintainer/discussions)
titled "Request for a private security contact" **with no details about the
vulnerability**, and a maintainer will open a private advisory and invite you
to it. (Blank issues are disabled, so do not misuse the bug or feature
templates for this.)

Maintainers: enable private vulnerability reporting under **Settings > Code
security** so the main path above works.

Please include:

- a description of the issue and its impact;
- steps to reproduce, or a minimal proof of concept;
- the affected files, routes or configuration (for example `MERGE_POLICY`,
  `ENABLE_TERMINAL`, `GITHUB_AUTH_MODE`);
- the commit you tested.

We will confirm receipt, work with you on a fix in the private advisory, and
credit you in the published advisory unless you prefer otherwise. This is a
volunteer-maintained project, so we cannot promise fixed response times.

## Scope notes

These behaviours are documented and are not vulnerabilities by themselves.
Reports that show they can be reached or bypassed in ways the documentation
does not describe are welcome.

- `MERGE_POLICY=auto` merges a pull request on the LLM Maintainer's approval
  and a trusted trigger without running any tests.
- With `ENABLE_TERMINAL=true`, the listed admins get a shell that runs as the
  API's OS user and can read the server's files and every clone.
- `AUTH_DEV_BYPASS=true` disables authentication outside
  `ENVIRONMENT=production`.
- `GITHUB_AUTH_MODE=pat` uses one token for every repository and is meant for
  single-tenant use only.
- `VERIFY_ALLOW_LOCAL=true` runs the target repository's tests on the worker
  host, where they can read the worker's secrets.
- With code retrieval indexing on (`RAG_INDEXING_ENABLED`), the target
  repository's code is sent to Vertex AI. With retrieval on
  (`RAG_RETRIEVAL_ENABLED`), the query, which is the issue text, is sent to
  the same embedder (see
  [docs/RAG.md](./docs/RAG.md#data-egress)).
- The evaluation harnesses run model-written code in a local `pytest` process
  without a sandbox (see [docs/EVALS.md](./docs/EVALS.md#limitations)).

## Security controls

A summary of the controls in the code. [ARCHITECTURE.md](./ARCHITECTURE.md),
[docs/VERIFICATION.md](./docs/VERIFICATION.md) and the
[deployment security checklist](./docs/DEPLOYMENT.md#security-checklist) have
the details.

**Authentication and tenancy**
- Every API route except the health probes, the OpenAPI docs and the signed
  GitHub webhook requires a Supabase access token
  (`backend/test_route_auth.py` enforces this). The static dashboard build,
  when the API serves it at `/`, is public; it holds no user data, only the
  compiled UI and its public `NEXT_PUBLIC_*` settings.
- Data is scoped to the caller's organization. Pending invites grant no access.
  Write routes require the `owner`, `admin` or `member` role, and org-wide
  actions require an admin.
- Every Supabase table has Row Level Security enabled. This includes
  `code_chunks`, the code retrieval index: members can read only their
  organizations' chunks, only the backend writes them, and a composite foreign
  key moves the chunks with their repository when it changes organization.
  The `ci-db` workflow checks the policies against PostgreSQL
  (`backend/tests/rls`). The backend's service-role key stays server-side, and
  the dashboard uses the anon key.
- `AUTH_DEV_BYPASS` is refused when `ENVIRONMENT=production`, which the Docker
  image sets by default.

**GitHub access**
- In `app` mode, each run and IDE request uses a short-lived installation
  token for the repository's own GitHub App installation. It never falls back
  to a global token.
- Linking an installation to an organization is verified with GitHub: the user
  must own or administer the installation's account.
- Webhooks are verified with HMAC-SHA256 (`X-Hub-Signature-256`) over the raw
  body; a missing or wrong signature gets a 401. Deliveries are de-duplicated
  by delivery id.
- Git receives tokens through its environment, not `.git/config` or argv, and
  stored credentials are scrubbed from existing clones.

**Merging**
- `MERGE_POLICY` is the deployment's ceiling. `human`, the default and the
  value used for unknown settings, never merges. A repository can only make
  the policy stricter, and a setting that cannot be read falls back to
  `human` (`backend/automaintainer/_runtime/merge_policy.py`).
- Under `ci` and `auto`, a merge also needs a trusted trigger
  (`backend/automaintainer/_runtime/trust.py`): every GitHub user who caused
  the run, and the author of a targeted issue, must have `write`, `maintain`
  or `admin` permission on the repository, checked by numeric user id. A
  failed lookup holds the merge.
- Under `ci` and `auto`, a merge is also refused when the review was not
  posted or the reviewed diff was incomplete, and it is pinned to the
  reviewed commit SHA.
- Under `ci`, a merge also requires the repository's tests to pass
  (`automaintainer/verification`) and every other check on the PR head to
  pass.
- Each merge decision is recorded in `merge_decisions`, and each change to a
  repository's merge policy in `repository_merge_policy_changes`.

**Agent pipeline**
- PM and Maintainer decisions are schema-validated JSON; an invalid reply
  gets one corrective retry and is then a rejection. An approval that lists
  blocking issues is invalid.
- Repository content, issue text, retrieved code chunks and the ADK
  Reviewer's tool results reach the model inside escaped `<untrusted-data>`
  blocks with an instruction to treat them as data. This reduces
  prompt-injection risk but does not remove it.
- The red-team fixtures in `backend/tests/redteam/` run the pipeline offline
  against a malicious issue body, instructions in a README or a code comment,
  a planted issue and a poisoned retrieval index, under each merge policy. The
  `redteam.yml` workflow runs them on pull requests that touch `backend/`.
- Change limits refuse a commit to a denied path, such as workflow files or
  test-runner configuration, before anything is pushed.
- Generated code is validated before commit (refusals, truncation, ambiguous
  fences, syntax for Python/JSON/TOML/YAML, and a shrink guard against
  truncated rewrites). LLM errors abort the run instead of being written to GitHub.
- The ADK Reviewer, when enabled, has read-only tools with path and size
  limits that refuse symlinks escaping its checkout.
- Run side effects are idempotent across task redeliveries.

**Verification of untrusted code**
- The target repository's tests are untrusted code. They run through a
  sandbox backend named by `SANDBOX_BACKEND`. Two backends ship: Docker
  (`backend/automaintainer/runners/docker_sandbox.py`) and Cloud Run Jobs
  (`backend/automaintainer/runners/cloud_run_job_sandbox.py`).
- Without a sandbox backend, verification refuses to run the tests on the
  API or worker host in every environment, production included, unless
  `VERIFY_ALLOW_LOCAL=true`. It reports an error instead, so `ci` does not
  merge. The Terraform configuration does not let `app_env` set
  `VERIFY_ALLOW_LOCAL`, and it selects the Cloud Run Jobs backend on the
  Google Cloud worker.
- The Cloud Run Jobs sandbox runs as a service account with no roles, in a
  network with no route to the worker, Redis or the internet, and exchanges
  data with the worker only through short-lived signed URLs for that run.

**Workspace and IDE**
- Repository names and file paths are validated, and paths that resolve
  outside the clone or into `.git/` are refused, including through symlinks.
- IDE writes go to the user's own working branch, never to the default branch
  or a protected branch.
- Inline assist has per-user and per-organization quotas.
- The terminal is off unless `ENABLE_TERMINAL=true`, limited to
  `TERMINAL_ADMIN_USER_IDS` (empty means nobody), checks the WebSocket origin,
  and gives the shell an allowlisted environment without server secrets. It is
  **not** a sandbox.

**Google Cloud deployment** (`infra/terraform`, opt-in)
- The deploy workflow authenticates through Workload Identity Federation,
  with no stored key. Google accepts the GitHub token only for this
  repository (its name, numeric repository id and owner id), the `production`
  environment, and the ref `refs/heads/main` or a `v*` tag. The deployer
  service account has no direct access to secrets and cannot change IAM.
  It can act as the API and worker service accounts and set the worker VM's
  metadata, so it can deploy code that runs as accounts that read secrets
  (`infra/terraform/iam.tf`).
- Secrets live in Secret Manager. Terraform creates the secret containers
  only, never a version, so no secret value is stored in the Terraform state.
  Each service account can read only the secrets it uses.
- Redis requires a password by default (`redis_auth_enabled`). The broker
  URL with the password is the version of the `redis-url` secret, which the
  owner adds; the worker refuses a URL without a password.
- `app_env` refuses `AUTH_DEV_BYPASS`, `ENABLE_TERMINAL`,
  `VERIFY_ALLOW_LOCAL`, `ENVIRONMENT`, `REDIS_URL` and `PORT`.

**Supply chain and CI**
- Gitleaks scans the full history on every push and pull request to `main`.
  CodeQL analyses Python and JavaScript/TypeScript. Bandit, pip-audit and
  `npm audit` also run in CI, and an OpenSSF Scorecard workflow runs as well.
- Every third-party GitHub Action is pinned to a full commit SHA
  (`backend/tests/test_workflow_hygiene.py` checks this).
- Backend dependencies are locked in `backend/uv.lock`. Dependabot covers uv,
  npm, GitHub Actions and Docker.
- The Docker image runs as a non-root user.

## Known gaps

These gaps are tracked in the security and safe-pipeline epics
([#420](https://github.com/PxA-Labs/AutoMaintainer/issues/420),
[#411](https://github.com/PxA-Labs/AutoMaintainer/issues/411)) and in the
Google Cloud deployment issue
([#527](https://github.com/PxA-Labs/AutoMaintainer/issues/527)):

- No sandbox backend is configured by default. Until an operator sets
  `SANDBOX_BACKEND`, verification reports an error and `MERGE_POLICY=ci`
  does not merge.
- The Cloud Run Jobs sandbox can still reach Google APIs through Private
  Google Access, so a test could upload the repository it was given. Only
  VPC Service Controls would close this (see
  [docs/VERIFICATION.md](./docs/VERIFICATION.md#cloud-run-jobs-sandbox-backend)).
  The backend has not yet been smoke-tested against a live project.
- On Google Cloud, the worker container uses the host network and can reach
  the metadata server, so untrusted code must never run on the worker
  itself.
- The terminal shell, the agent worker and the API share one OS user, so the
  terminal has no isolation between tenants.
- A webhook row left in `processing` by a crashed worker is not requeued
  automatically.
