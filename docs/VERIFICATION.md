# Verifying agent changes

Before this feature, the only check on an agent change was the Maintainer LLM
reviewing code that the same pipeline generated. Verification adds evidence
the model cannot produce: it runs the target repository's own tests on the
exact commit the Implementer pushed, and records the result on the pull
request. This is the MVP of the "sandboxed test run" item of epic #411.

## How it works

```
Implementer pushes the change to automaintainer/<run> and opens/updates the PR
        |
        v
agents._verify_and_report  (once per iteration, and again when a
        |                           redelivered run resumes a pushed iteration)
        |  head SHA = tip of the PR branch
        v
verification.verify_pr_branch
        |  shallow clone of that branch into a temp dir, refuse if HEAD != head SHA
        v
verification.verify_working_copy
        |  pick the test command, run it (sandbox backend or restricted local process)
        v
VerificationResult {status, summary, command, duration_seconds,
                    exit_code, output_tail, truncated, runner, head_sha}
        |
        +--> commit status "automaintainer/verification" on the head SHA
        +--> "Verification" section in the PR body (replaced on every iteration)
        +--> TestResultEvent (phase implementer) and a run log line
```

The temporary clone is deleted afterwards. The GitHub token reaches `git
clone` through its environment only (`workspace.git_auth_env`); the test
process never receives it.

### Status values

| Status    | Meaning                                                                 | Commit status |
| --------- | ----------------------------------------------------------------------- | ------------- |
| `passed`  | The test command exited 0.                                              | `success`     |
| `failed`  | The test command exited non-zero.                                       | `failure`     |
| `error`   | No trustworthy answer: timeout, local execution refused (no sandbox and no opt-in), invalid config, clone failed, branch moved, sandbox failure. | `error` |
| `skipped` | Nothing ran: no tests detected, or `VERIFY_CHANGES=false`.              | `error`       |

### Merge gating

`MERGE_POLICY=ci` merges only when the `automaintainer/verification` status
on the PR head is present and `success`, and every other commit status and
check run on that head is green (`agents._ci_checks_passed`). Only `passed`
maps to `success`; `failed`, `error`, `skipped` **and a missing status** all
block the merge, so a crash, a skipped step or a status that never got posted
cannot let the repository's other CI merge an unverified head. Because the
status is bound to a SHA, a later push to the branch is never merged on an
older result.

If the status cannot be posted while `MERGE_POLICY=ci` (GitHub refuses the
write, or the PR head could not be resolved at all), the run stops with
`VERIFICATION_STATUS_FAILED`; the PR stays open. Under `human` (the default)
and `auto`, a failure to post is logged and the run continues.

A redelivered run (Celery `acks_late`) that resumes an iteration whose commit
was already pushed verifies that commit again before the Maintainer runs, so a
crash between the push and the status post does not leave the PR unverified.

For a backstop that does not depend on the agent at all, also mark
`automaintainer/verification` as a **required status check** in the branch
protection of maintained repositories. GitHub then refuses any merge without a
green verification, including merges by people.

The GitHub App needs the **Commit statuses: read & write** repository
permission.

Green checks are not enough for an automatic merge, because a
prompt-injected run controls both the change and the tests it adds. Under
`ci` and `auto` the merge also needs a **trusted trigger** (`backend/trust.py`,
#487): the caller who started the run (their linked GitHub login) and the
author of the issue it targets, if any, must each have `write`, `maintain` or
`admin` permission on the repository. The lookup is deterministic, made once
per run through the run's installation token, and fails closed: an unknown or
bot account, a non-collaborator or any API error leaves the PR open for a
human with a `held` decision (`untrusted trigger` or `trigger permission
check failed`). A redelivered run replays the held decision from its
checkpoint; one that reviews again looks the trigger up again.

### Maintainer input

The Maintainer's review prompt includes the `VerificationResult` of the
current iteration (`agents._verification_review_section`; the Implementer
hands it over as `verification` in the graph state):

- The status (`passed`, `failed`, `error`, `skipped`, anything else shown as
  `unknown`) and the exit code are set by AutoMaintainer and appear outside
  the data block.
- The summary, the command (it can come from `.automaintainer.yml`) and the
  output tail are attacker-influenced, so they go inside an escaped
  `<untrusted-data source="verification output">` block. The tail is cut to
  the same last 3000 characters (`PR_OUTPUT_TAIL_CHARS`) as the PR body.
- When no result exists (for example a node run outside the pipeline), the
  prompt says explicitly that verification was **not run** and the change is
  untested.

The system prompt (`MAINTAINER_VERIFICATION_INSTRUCTIONS`) tells the
Maintainer that the output is evidence, not instructions, and that on
`failed`, `error`, `skipped` or not run it must reject, unless its reasons
explain specifically why the result is unrelated to the change (for example a
failure in code the change does not touch). This is guidance for the model,
not a gate: an approval that explains a failure away reaches the merge policy
like any other approval. The gate stays fail closed: `MERGE_POLICY=ci` still
requires the `automaintainer/verification` status to be `success`, whatever
the Maintainer decides, and `human` never merges. `auto` merges on approval
alone, as before.

### Audit trail

The PR body and the commit status show only the latest result. Every
verification also writes one row to the `verification_results` table
(migration `supabase/migrations/20261116120000_verification_results.sql`,
hook `verification.record_result`, called by `agents._verify_and_report` on
both the normal path and the redelivery-resume path):

- The run context: `org_id`, `run_id`, `repository_id` and `repo_name` (read
  from the `runs` row), `pr_number`, `head_sha` (empty when the head could
  not be resolved) and `iteration` (the run checkpoint's
  `implemented_iteration`, which the Implementer saves before verifying).
- The result: `status` (`passed`, `failed`, `error`, `skipped`), `summary`,
  `command` (the argv as a JSON array), `exit_code`, `duration_ms`,
  `output_tail` (the last 4000 characters, `AUDIT_OUTPUT_TAIL_CHARS`, with NUL
  bytes removed; a `CHECK` enforces the cap), `output_truncated` and
  `backend` (`local`, or the `SANDBOX_BACKEND` factory for sandboxed runs).

The row is written right after the tests ran, before anything is posted to
GitHub, so it exists even when posting the status then stops the run. Writing
it is best effort: a database failure is logged with the run id and changes
neither the status, the PR section, the Maintainer input nor the merge
decision. A run without a `runs` row (no database, local scripts) writes
nothing.

Rows are not deduplicated. A redelivered run that verifies the same
`head_sha` again ran the tests again, so it writes a second row; two rows
with the same `run_id` and `head_sha` mean the commit was tested twice.

Org members read their organization's rows through
`GET /runs/{run_id}/verifications` (newest first, at most 100; same auth and
tenant check as `GET /status/{run_id}`, so another organization's run is a
404) or directly under RLS. Only the service role writes them; there is no
INSERT, UPDATE or DELETE policy for `anon` or `authenticated`. The table is
not in the `supabase_realtime` publication, because the dashboard does not
subscribe to it. Rows are deleted with their run or organization.

### Merge decisions audit

Each Maintainer review of a PR ends in a decision, and `maintainer_node`
records it as one `merge_decisions` row (migration
`supabase/migrations/20261116180000_merge_decisions.sql`, hook
`merge_audit.record_decision`) with the run context, the reviewed `head_sha`,
the `iteration`, the `policy` in force (`human`, `ci`, `auto`; an unknown
`MERGE_POLICY` is recorded as `human`) and:

| Branch | `decision` | `actor` | Reason recorded |
|---|---|---|---|
| Maintainer rejects (or its reply is invalid) | `not_merged` | `agent`, `actor_role` `Maintainer` | `maintainer rejected` |
| `MERGE_POLICY=human` | `held` | `policy` | `policy human` |
| `ci`: verification or checks not green | `held` | `policy` | `ci status not success: ...` |
| Review comment not posted / diff incomplete / change limits | `held` | `policy` | `review comment not posted`, `reviewed diff incomplete`, `change limits exceeded` |
| `ci`/`auto`: trigger not a repository writer, or its lookup failed | `held` | `policy` | `untrusted trigger` / `trigger permission check failed` |
| A later iteration hits a denied path (nothing committed, the run fails with `PROTECTED_PATH`) | `held` | `policy` | `change limits exceeded`, `protected change not committed` |
| `auto`, or `ci` with green checks, and a trusted trigger | `merged` | `policy` | `policy auto` / `ci checks passed: ...`, then `trusted trigger` |
| GitHub refuses the merge (the run then fails) | `not_merged` | `policy` | `github did not merge the pr` |

`reasons` lists the gates in the order they ran (at most 10, 200 characters
each). `evidence` holds the PR link, the reviewed commit's page (where the
`automaintainer/verification` status is listed; it has no `target_url`), the
verification result the Maintainer saw and the id of the newest
`verification_results` row for that head. `actor` `human` is allowed by the
table but not written yet. A run without a PR writes no row.

A redelivered run that replays a review from its checkpoint writes nothing.
One that reviews again (it crashed before saving the checkpoint) made a new
decision and writes a new row; if the first attempt had already merged, the
reason `already merged by an earlier attempt` says so. Writing is best effort
and has the same guarantees, RLS and read access as verification rows:
`GET /runs/{run_id}/decisions`, newest first, at most 100.

### Effect on existing deployments

- With no `SANDBOX_BACKEND` and no `VERIFY_ALLOW_LOCAL=true` (the shipped image
  today, in any environment), every agent PR gets an `error` verification status (a red X on the
  PR), whatever the merge policy, and `MERGE_POLICY=ci` no longer merges.
  Configure a sandbox backend, or accept the risk with `VERIFY_ALLOW_LOCAL=true`.
- A `MERGE_POLICY=ci` deployment whose GitHub App lacks **Commit statuses:
  write** now stops every run with `VERIFICATION_STATUS_FAILED`.
- `MERGE_POLICY=ci` with `VERIFY_CHANGES=false` never merges (the result is
  `skipped`).
- These settings are also listed in the environment reference in
  docs/DEPLOYMENT.md.

## Configuration

### Per repository: `.automaintainer.yml`

At the repository root:

```yaml
test_command: ["python", "-m", "pytest", "-q"]   # argument list, no shell
timeout_seconds: 600                              # optional
```

- `test_command` must be a non-empty list of strings. A plain string is
  rejected, because splitting it would need a shell.
- `timeout_seconds` must be a positive integer; it is capped by
  `VERIFY_MAX_TIMEOUT_SECONDS`.
- An invalid file is an `error`, not a silent fallback to detection.
- A change that modifies `.automaintainer.yml` itself is reported as `error`,
  so a change cannot rewrite the configured command. This guards that file
  only; see Limitations for the test files and runner configuration a change
  can still alter.
- The same file can also hold a `change_limits` mapping (maximum changed
  files and lines, denied paths). It is read from the base branch, never from
  the change; see [Change limits](../ARCHITECTURE.md#change-limits). Test
  runner configuration is denied by default; to let the agent edit, for
  example, `pyproject.toml`, add
  `change_limits: {allow: ["pyproject.toml"]}` on the base branch.

Without the file, the command is detected, first match wins:

1. **pytest**, when there is a `tests/` directory, a `pytest.ini`, a
   `[tool.pytest...]` table in `pyproject.toml`, or `[tool:pytest]` in
   `setup.cfg`: `<VERIFY_PYTHON> -P -c <launcher> -q`, where the launcher
   appends the repository root to the **end** of `sys.path` and runs
   pytest. Under plain `python -m pytest` the root comes first, so a root
   module named after anything pytest imports (`pytest.py`, `pluggy.py`, or
   a standard-library name such as `argparse.py`) would replace it and could
   make a failing suite exit 0. With the root last, installed and
   standard-library modules always win, and tests can still import packages
   from the repository root. `-P` needs Python 3.11 or later.
2. **npm**, when `package.json` has a `scripts.test` other than the `npm init`
   placeholder: `npm test --silent`.
3. Otherwise `skipped` with "no tests detected".

### Server settings

| Variable | Default | Effect |
| --- | --- | --- |
| `VERIFY_CHANGES` | on | `false` turns verification off. The result is then `skipped`, which still blocks `MERGE_POLICY=ci`. |
| `SANDBOX_BACKEND` | unset | `package.module:factory`. The factory is called with the host path of the working copy and returns a `runners.sandbox.SandboxBackend`. A set but unloadable value is an `error`; it never falls back to local execution. |
| `VERIFY_ALLOW_LOCAL` | unset | `true` allows running repository tests on the API/worker host without a sandbox, in any environment. Only for a developer machine running repositories you trust. |
| `VERIFY_TIMEOUT_SECONDS` | `300` | Default wall-clock limit for the test command. |
| `VERIFY_MAX_TIMEOUT_SECONDS` | `900` | Upper bound for a repository's `timeout_seconds`. Keep it well below the agent task's Celery `soft_time_limit` (1800 s in `tasks.py`): verification shares that budget with the clone (up to 300 s) and the LLM calls of every iteration, and a run that hits the limit ends before any result is posted. |
| `VERIFY_MAX_OUTPUT_BYTES` | `65536` | Output kept (the tail). The PR body shows the last 3000 characters. |
| `VERIFY_PYTHON` | `python` | Interpreter used by the detected pytest command. Must be Python 3.11 or later (`-P`); with an older one the detected command fails. |

## Security model

The test command executes code from the target repository, including code the
agent just wrote from an untrusted issue. Treat it as hostile.

- **A sandbox is required unless you opt in.** Without `SANDBOX_BACKEND`,
  verification refuses to run (`error`, so `ci` does not merge) unless
  `VERIFY_ALLOW_LOCAL=true`, in **every** environment. The refusal happens
  before the repository is cloned. This does not depend on `ENVIRONMENT`
  (which defaults to `development`), so a deployment that forgets to set it
  is not silently running untrusted code on the host; the web terminal
  follows the same opt-in rule (`ENABLE_TERMINAL`).
- **The local runner is defense in depth, not isolation.** It runs an
  argument list (no shell) in the temporary clone, with stdin closed, an
  environment reduced to `PATH` and a few OS variables (no API keys, database
  URLs or tokens), `HOME`/`TMP` pointed at an empty scratch directory, a
  wall-clock timeout that kills the whole process tree, and an output cap. It
  does **not** stop the tests from reading files the server user can read,
  using the network, or consuming CPU and memory up to the timeout. Use it for
  development only.
- **Scrubbing the environment does not hide the server's secrets.** The test
  process runs as the same OS user as the worker, so it can read them another
  way: on Linux from `/proc/<worker pid>/environ`, and from the server's
  `.env`, key files or any other file that user can read. With
  `VERIFY_ALLOW_LOCAL=true`, assume that a malicious repository obtains every
  secret of the worker (GitHub App key, Supabase service key, LLM keys).
- **The process-tree kill can be escaped.** On POSIX the runner kills the
  test's process group; a test that calls `setsid` or double-forks leaves that
  group and survives the timeout. Only a sandbox backend bounds this.
- **A sandbox backend** should run the command in an ephemeral container or
  micro-VM with no secrets, network egress denied (or limited to package
  mirrors) and CPU, memory and time limits. Those properties are the backend's
  responsibility; `runners/sandbox.py` defines the interface and
  `runners/docker_sandbox.py` ships one implementation (see
  [Docker sandbox backend](#docker-sandbox-backend)). The host waits at
  most `timeout_seconds` + 30 s for the backend, then reports `error`. The
  host does not bound memory for the backend's reply: `exec_command` returns
  stdout and stderr as whole strings, which are only then cut to
  `VERIFY_MAX_OUTPUT_BYTES`, so a backend should cap its own output (the
  Docker backend does).
- **Output is untrusted.** In the PR body it is placed in a code fence longer
  than any backtick run it contains, so it cannot inject Markdown or
  `@mentions`.

### Red-team fixtures

`backend/tests/redteam/` runs the real agent pipeline offline against three
prompt-injection vectors (epic #411): a malicious issue body, instructions in
the README and instructions in a code comment of the target file. A scripted
model either obeys the payload (compliant attacker) or ignores it, under each
`MERGE_POLICY`, with every other CI check green so `ci` would merge. The
tests assert the deterministic layer that stops each attack: a
`.github/workflows/` path is never chosen as a target (the run stops with
`NO_TARGET_FILE`), a denied path such as `conftest.py` is never committed
(the run fails with `PROTECTED_PATH` and opens no PR, #488), a compliant
model's approved in-limits change to an unprotected file is held by the
trusted-trigger gate (#487) because the issue author, or the account that
started the run, has only read access, and the payload reaches prompts only
inside escaped `<untrusted-data>` blocks. The fake GitHub answers the
collaborator-permission endpoint; control tests show the same well-behaved
change merging under `ci` and `auto` when a writer starts the run, and a
failed permission lookup holding it. Every decision writes one
`merge_decisions` row into the fake database, and the tests check its
decision and reasons. One test pins the gate's documented limit: a payload
in the README of a run a writer started still merges under `ci`/`auto` if a
model obeys it, since only a writer could have put it there; only `human`
prevents that. The `.github/workflows/redteam.yml` workflow runs them on
every pull request that touches `backend/`.

## Docker sandbox backend

`runners/docker_sandbox.py` is the shipped production backend. Each
`exec_command` starts one ephemeral container with the `docker` CLI (an
argument list, never a shell; no Docker SDK dependency), waits for it on the
host and returns its exit code and output.

### Configuration

```
SANDBOX_BACKEND=runners.docker_sandbox:create
SANDBOX_IMAGE=ghcr.io/your-org/test-runner@sha256:<digest>   # recommended
```

| Variable | Default | Effect |
| --- | --- | --- |
| `SANDBOX_IMAGE` | `python:3.12-slim` | Image the tests run in. The default has only the standard library: it does not contain pytest or node. Build an image with the tools your repositories need (dependencies are not installed at run time, and the network is off) and pin it by digest (`name@sha256:...`) so a re-tagged image cannot change what runs. |
| `SANDBOX_NETWORK` | `none` | Docker network for the container. Name a network that only reaches a package mirror if tests must install packages. `host` is refused. |
| `SANDBOX_RUNTIME` | unset (Docker's default, usually `runc`) | `runsc` runs the container under gVisor (must be installed and registered with the daemon). Recommended. |
| `SANDBOX_MEMORY` | `1g` | `--memory`; swap is disabled (`--memory-swap` equals it). |
| `SANDBOX_CPUS` | `1` | `--cpus`. |
| `SANDBOX_PIDS_LIMIT` | `256` | `--pids-limit`, against fork bombs. |
| `SANDBOX_TMPFS_SIZE` | `256m` | Size of the writable `/tmp` (also `HOME`). |
| `SANDBOX_WORKSPACE_SIZE` | `1g` | Size of the `/workspace` tmpfs the copy is unpacked into. It must hold the whole working copy, `.git` included, plus what the tests write there. tmpfs pages count against `SANDBOX_MEMORY`, so raise both for large repositories. |
| `SANDBOX_USER` | `65534:65534` (nobody) | Numeric, never `0`. |
| `SANDBOX_DOCKER` | `docker` | Path to the CLI (any CLI accepting the same `run` flags). |
| `VERIFY_MAX_OUTPUT_BYTES` | `65536` | Also caps the output the backend keeps while reading. |

Every value is validated and passed as `--flag=value`, so a setting cannot
inject another docker flag. Memory, CPU and tmpfs sizes must be greater than
zero (docker reads `0` as "unlimited"). The image must provide `/bin/sh` and
`tar` (the `python` and most distribution images do; distroless images do
not).

### Deployment

The worker needs the `docker` CLI (the shipped image does not include it)
and access to a daemon. The daemon does **not** need to see the worker's
filesystem: the working copy is streamed to the container over the CLI
connection, never bind-mounted. Any of these works:

- the worker and the daemon on one dedicated sandbox host (recommended:
  run the `agent_runs` worker there, next to its daemon);
- the worker elsewhere, with `DOCKER_HOST=tcp://...` (TLS:
  `DOCKER_TLS_VERIFY`, `DOCKER_CERT_PATH`) or `DOCKER_HOST=ssh://user@host`
  pointing at a dedicated sandbox host;
- the worker in a container (as in `docker-compose.yml`) talking to a daemon
  outside it.

The docker CLI process inherits only `PATH`, `HOME`, `USER`, the `DOCKER_*`
variables, `XDG_RUNTIME_DIR`, `SSH_AUTH_SOCK` (for `ssh://`), the
`HTTP(S)_PROXY`/`NO_PROXY` variables (either case) and, on Windows, the
system variables it needs. Use a docker context or `DOCKER_CONFIG` for
anything else.

**Rootless Docker** works with the defaults: nothing the container writes
reaches the worker's disk, so subordinate uids do not matter. `SANDBOX_USER`
is never `0`, even though uid 0 in a rootless daemon is unprivileged on the
host, because the same setting would be real root under a rootful daemon.

### What one run does

```
pack the working copy into a tar archive (symlinks kept as links, never followed;
  in memory up to 8 MiB, then an anonymous temp file on the worker)
docker run --rm --interactive --name=automaintainer-sandbox-<uuid>
  --network=none --read-only --tmpfs=/tmp:rw,nosuid,nodev,size=256m
  --cap-drop=ALL --security-opt=no-new-privileges
  --pids-limit=256 --memory=1g --memory-swap=1g --cpus=1
  --user=65534:65534 --workdir=/workspace
  --tmpfs=/workspace:rw,exec,nosuid,nodev,mode=1777,size=1g
  --entrypoint=/bin/sh [--runtime=runsc] --env=HOME=/tmp ...
  <image> -c 'tar -xf - -C /workspace || exit 125; exec "$@" </dev/null'
  automaintainer-sandbox <test command>
stream the archive on stdin, then close it
on timeout, error or cancellation: kill the CLI, then docker rm --force <name>
  (retried once)
```

- **The host clone is never mounted.** Each container unpacks its own copy
  into a tmpfs that disappears with it, so a test cannot change files (or
  plant git configuration) that a later step reads. `get_git_diff` runs `git
  diff --no-ext-diff --no-textconv HEAD` on the host clone. The test command
  is passed to `sh` as arguments (`"$@"`), never interpolated into the
  script.
- **Environment:** only `HOME`/`TMPDIR=/tmp`, `CI=true`, `NO_COLOR`,
  `PYTHONDONTWRITEBYTECODE`, `PYTHONUNBUFFERED`, `GIT_TERMINAL_PROMPT=0` and a
  `safe.directory` git setting; `PATH` comes from the image. Nothing from
  the worker's environment is passed, and the docker CLI process itself only
  gets the variables it needs to reach the daemon (see Deployment).
- **Timeout:** enforced by the worker and counted from the start of packing
  the copy, so `exec_command` returns within `timeout_seconds` plus cleanup.
  When it expires the docker CLI is killed first (so it cannot still create
  or start the container), then the container is force-removed, and the
  result is `error` (timed out). `--rm` removes it on a normal exit.
- **Output:** stdout and stderr interleaved, cut to the last
  `VERIFY_MAX_OUTPUT_BYTES` while reading, with a truncation note.
- **Exit code 125** (docker could not start the container: missing image,
  unknown runtime, daemon down; or the copy could not be unpacked, e.g. it
  exceeds `SANDBOX_WORKSPACE_SIZE`) is reported as `error`, not as a failing
  test. A test command that itself exits 125 is therefore reported as
  `error` too; both block `MERGE_POLICY=ci`, so a repository gains nothing
  by it.
- File operations act on the host working copy and refuse paths that escape
  it. `commit_and_push` raises `NotImplementedError`: verification never
  pushes.

### Threat model

Isolated from the tests:

- the worker's secrets and environment variables, and all of its files (no
  host mount at all; the copy arrives on stdin);
- the network, unless `SANDBOX_NETWORK` opts into one;
- the host clone and later pipeline steps (copy-in);
- privilege escalation inside the container (no capabilities,
  `no-new-privileges`, non-root user, read-only root filesystem);
- resource exhaustion beyond the memory, CPU, pids, `/tmp`, `/workspace`
  and time limits.

Not isolated:

- **Access to the Docker daemon is root-equivalent on the daemon's host.**
  The worker that holds the socket (or `DOCKER_HOST` credentials) can start a
  privileged container at will, so a compromised worker is a compromised
  daemon host. Do not mount the production host's socket into the worker.
  Prefer a **dedicated sandbox host** (a VM with nothing else on it): run the
  verification worker on it, or reach its daemon from the worker through
  `DOCKER_HOST` (see Deployment). Use **rootless Docker** and/or gVisor
  there as well.
- **Kernel escapes.** With the default `runc` runtime the tests share the
  host kernel; a kernel vulnerability escapes the container. `SANDBOX_RUNTIME=runsc`
  (gVisor) puts a user-space kernel in between; a micro-VM backend
  (Firecracker) is stronger still.
- **What the tests print.** Output is untrusted and handled as described
  above; with a non-`none` network a test can also exfiltrate the repository
  contents (which it could read anyway) to whatever that network reaches.
- **The worker's own disk and memory while packing.** The archive is built
  on the worker (in memory up to 8 MiB, then in an anonymous temp file) and
  is as large as the working copy. It is not bounded by the sandbox limits,
  but tests cannot grow it: they never write to the worker.
- **The change influencing its own tests** (see Limitations): the sandbox
  bounds what the tests can do to the host, not whether they are honest.

### Testing

`backend/test_docker_sandbox.py` mocks the docker CLI and checks the exact
argument list (no bind mount), the archive streamed on stdin, the absence of
host variables, the timeout cleanup order (CLI kill before `docker rm`), the
output cap and cleanup on errors. `backend/test_docker_sandbox_integration.py`
runs real containers and is skipped unless Docker can run Linux containers
and `DOCKER_SANDBOX_TESTS=1`; the `Sandbox Integration` workflow
(`.github/workflows/sandbox.yml`) runs it on `ubuntu-latest` whenever
`backend/runners/**` or `backend/verification.py` change: a passing, a
failing and a hanging repository, and one whose tests try the network and a
host environment variable. It runs them twice: against the runner's daemon,
and against a separate Docker-in-Docker daemon reached through
`DOCKER_HOST=tcp://...`, which shares no filesystem with the worker (the
remote sandbox host setup).

## Limitations

- **Passing tests are evidence, not proof.** They cover only what the
  repository tests; the PR section says so. A repository without tests gets
  `skipped`, never `passed`.
- **Dependencies are not installed.** The command runs as is. For a detected
  pytest run the repository's own dependencies must already be importable by
  `VERIFY_PYTHON`, or the run fails at import. Repositories that need an
  install step should set `test_command` (for example a script that installs
  and tests) and run under a sandbox image prepared for them.
- **Only the first detected tool runs.** A repository with both pytest and
  npm tests runs pytest unless `test_command` says otherwise. pytest's
  "no tests collected" (exit 5) counts as `failed`.
- **The PR is opened before the result is attached.** Verification runs right
  after the push; the PR body section and status appear moments later. The
  merge gate is unaffected because it reads the status on the head SHA.
- **The change can still influence its own tests.** Verification itself only
  protects `.automaintainer.yml`. The default change-limit denylist also holds
  test-runner configuration for a human (#480): `conftest.py` at any depth,
  `pytest.toml`, `.pytest.toml`, `pytest.ini`, `.pytest.ini`, `tox.ini`,
  `noxfile.py`, `setup.cfg`, `pyproject.toml`, `package.json`,
  `jest.config.*`, `vitest.config.*`, `vitest.workspace.*`,
  `vitest.projects.*`, `vite.config.*` (Vitest falls back to its `test`
  block), `.mocharc*`, `karma.conf.*` and `playwright.config.*`. It also
  holds files that replace the runner for custom commands such as
  `python -m pytest` (`-m` puts the working directory first on `sys.path`,
  so a root `pytest.py` or `pytest/` package would run instead of pytest and
  could print a pass); `pytest.py`, `pytest/**` and `_pytest/**` are denied
  at any depth. npm reads the
  project `.npmrc` when it runs `npm test` (`script-shell=true` would make it
  exit 0 without running the script), so `.npmrc` is denied too. A change to
  one of them gets a draft `needs-human` PR and is never merged, even when
  verification passes. Test files themselves (`tests/**`, `test_*.py`,
  `*.test.*`) are **not** denied, because writing and fixing tests is part of
  the agent's job. Nor is any other file the tests load: setup files
  (`jest.setup.js`, `tests/__init__.py`), helpers and the source under test
  can monkeypatch the runner much as `conftest.py` can, and setup-file names
  are configurable, so denying by name is a partial guard. The same goes for
  build configuration the runner applies, such as `babel.config.js` or
  `tsconfig.json`. Denying by name cannot stop a root module that shadows
  anything pytest imports, standard library included (`argparse.py`,
  `inspect.py`): under plain `python -m pytest` such a file makes a failing
  suite exit 0. The detected command puts the root last on `sys.path`
  instead (see the detection rules above); a custom `test_command` should
  run `python -P` and add the root the same way, or use the `pytest`
  executable. A change can still
  weaken, skip or delete assertions in a test file it edits and make
  verification pass. No default entry protects a custom `test_command` (a
  script, `make test`, `tox`): add its entry point to `change_limits.deny`
  on the base branch, e.g. `deny: ["scripts/test.sh", "Makefile"]`. The
  Maintainer now sees the verification output (see [Maintainer input](#maintainer-input)), but that
  is a model reading untrusted output, not a guarantee. Human review, or
  required CI that the change cannot modify, remains necessary for
  `MERGE_POLICY=ci`/`auto`. A repository whose fixes routinely touch, say,
  `pyproject.toml` can re-allow it with `change_limits.allow` on the base
  branch (see [Per repository](#per-repository-automaintaineryml)).
- **Event counts are synthetic.** The `TestResultEvent` reports
  `passed_count`/`failed_count` as 1/0 for the whole command; individual test
  counts are not parsed from the output.
- **Re-verification on resume repeats the run.** A redelivered run verifies
  the pushed commit again even if the crashed attempt had already posted a
  result; this costs one more test run and keeps the result fresh.
- **Verification can make `ci` merge in a repository with no other CI.**
  `_ci_checks_passed` requires at least one successful check; a passed
  verification is one. Repositories that want an independent CI signal as
  well should make their CI job a required check in branch protection.
- **Audit rows are best effort.** A verification whose row could not be
  written (database unavailable, run without a `runs` row) leaves only a log
  line; the PR and the commit status still carry the result (see
  [Audit trail](#audit-trail)). The output kept there is a 4000-character
  tail, not the full log.
- **Windows hosts** kill the process tree with `taskkill /T`; POSIX hosts use
  a new session and `killpg`.
