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
  alone.
- With `ENABLE_TERMINAL=true`, the listed admins get a shell that runs as the
  API's OS user and can read the server's files and every clone.
- `AUTH_DEV_BYPASS=true` disables authentication outside
  `ENVIRONMENT=production`.
- `GITHUB_AUTH_MODE=pat` uses one token for every repository and is meant for
  single-tenant use only.
- The eval harness runs model-written code in a local `pytest` process without
  a sandbox (see [docs/EVALS.md](./docs/EVALS.md#limitations)).

## Security controls

A summary of the controls in the code. [ARCHITECTURE.md](./ARCHITECTURE.md)
has the details.

**Authentication and tenancy**
- Every API route except the health probes, the OpenAPI docs and the signed
  GitHub webhook requires a Supabase access token
  (`backend/test_route_auth.py` enforces this). The static dashboard build,
  when the API serves it at `/`, is public; it holds no user data, only the
  compiled UI and its public `NEXT_PUBLIC_*` settings.
- Data is scoped to the caller's organization. Pending invites grant no access.
  Write routes require the `owner`, `admin` or `member` role, and org-wide
  actions require an admin.
- Every Supabase table has Row Level Security enabled. The backend's
  service-role key stays server-side, and the dashboard uses the anon key.
- `AUTH_DEV_BYPASS` is refused when `ENVIRONMENT=production`, which the Docker
  image sets by default.

**GitHub access**
- In `app` mode, each run and IDE request uses a short-lived installation
  token for the repository's own GitHub App installation. It never falls back
  to a global token.
- Linking an installation to an organization is verified with GitHub: the user
  must own or administer the installation's account.
- Webhooks are verified with HMAC-SHA256 (`X-Hub-Signature-256`) over the raw
  body. Deliveries are de-duplicated by delivery id.
- Git receives tokens through its environment, not `.git/config` or argv, and
  stored credentials are scrubbed from existing clones.

**Agent pipeline**
- `MERGE_POLICY=human` is the default: agents never merge their own pull
  requests. Under `ci` and `auto`, a merge is also refused when the review was
  not posted or the reviewed diff was incomplete, and it is pinned to the
  reviewed commit SHA.
- PM and Maintainer decisions are schema-validated JSON; an invalid reply
  gets one corrective retry and is then a rejection. An approval that lists
  blocking issues is invalid.
- Under `MERGE_POLICY=ci`, a merge also requires the repository's tests to
  pass (`automaintainer/verification`). Those tests are untrusted code: they
  run in a sandbox, and on the host only with `VERIFY_ALLOW_LOCAL=true`.
- Repository and issue content reaches the model inside escaped
  `<untrusted-data>` blocks with an instruction to treat it as data. This
  reduces prompt-injection risk but does not remove it.
- Generated code is validated before commit (refusals, truncation, ambiguous
  fences, syntax for Python/JSON/TOML/YAML, and a shrink guard against
  truncated rewrites). LLM errors abort the run instead of being written to GitHub.
- Run side effects are idempotent across task redeliveries.

**Workspace and IDE**
- Repository names and file paths are validated, and paths that resolve
  outside the clone or into `.git/` are refused, including through symlinks.
- Inline assist has per-user and per-organization quotas.
- The terminal is off unless `ENABLE_TERMINAL=true`, limited to
  `TERMINAL_ADMIN_USER_IDS` (empty means nobody), checks the WebSocket origin,
  and gives the shell an allowlisted environment without server secrets. It is
  **not** a sandbox.

**Supply chain and CI**
- CodeQL, Gitleaks, Bandit and pip-audit run in CI, and an OpenSSF Scorecard
  workflow runs as well.
- Backend dependencies are locked in `backend/uv.lock`. Dependabot covers uv,
  npm, GitHub Actions and Docker.
- The Docker image runs as a non-root user.

## Known gaps

These gaps are tracked in the security and safe-pipeline epics
([#420](https://github.com/PxA-Labs/AutoMaintainer/issues/420),
[#411](https://github.com/PxA-Labs/AutoMaintainer/issues/411)):

- The target repository's tests are not run before review or merge.
- The terminal shell, the agent worker and the API share one OS user, so the
  terminal has no isolation between tenants.
- A webhook row left in `processing` by a crashed worker is not requeued
  automatically.
