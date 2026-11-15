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

Without the file, the command is detected, first match wins:

1. **pytest**, when there is a `tests/` directory, a `pytest.ini`, a
   `[tool.pytest...]` table in `pyproject.toml`, or `[tool:pytest]` in
   `setup.cfg`: `<VERIFY_PYTHON> -m pytest -q`.
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
| `VERIFY_PYTHON` | `python` | Interpreter used by the detected pytest command. |

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
| `SANDBOX_USER` | the worker's `uid:gid`, or `65534:65534` when the worker is root or on Windows | Numeric, never `0`. A non-root worker must own files the tests create in the copy to delete it afterwards. |
| `SANDBOX_DOCKER` | `docker` | Path to the CLI (any CLI accepting the same `run` flags). |
| `VERIFY_MAX_OUTPUT_BYTES` | `65536` | Also caps the output the backend keeps while reading. |

Every value is validated and passed as `--flag=value`, so a setting cannot
inject another docker flag. The worker needs the `docker` CLI (the shipped
image does not include it) and access to a daemon, via the socket or
`DOCKER_HOST`.

### What one run does

```
copy the working copy -> private temp dir (symlinks kept as links, never followed)
docker run --rm --name=automaintainer-sandbox-<uuid>
  --network=none --read-only --tmpfs=/tmp:rw,nosuid,nodev,size=256m
  --cap-drop=ALL --security-opt=no-new-privileges
  --pids-limit=256 --memory=1g --memory-swap=1g --cpus=1
  --user=<uid:gid> --workdir=/workspace
  --mount=type=bind,source=<copy>,target=/workspace
  [--runtime=runsc] --env=HOME=/tmp ... <image> <test command>
on timeout, error or cancellation: docker rm --force <name>
delete the copy
```

- **The host clone is never mounted.** The container gets a fresh copy per
  command, deleted afterwards, so a test cannot change files (or plant git
  configuration) that a later step reads. `get_git_diff` runs `git diff
  --no-ext-diff --no-textconv HEAD` on the host clone.
- **Environment:** only `HOME`/`TMPDIR=/tmp`, `CI=true`, `NO_COLOR`,
  `PYTHONDONTWRITEBYTECODE`, `PYTHONUNBUFFERED`, `GIT_TERMINAL_PROMPT=0` and a
  `safe.directory` git setting; `PATH` comes from the image. Nothing from
  the worker's environment is passed, and the docker CLI process itself only
  gets the variables it needs to find the daemon.
- **Timeout:** enforced by the worker. When it expires the container is
  force-removed and the result is `error` (timed out). `--rm` removes it on a
  normal exit.
- **Output:** stdout and stderr interleaved, cut to the last
  `VERIFY_MAX_OUTPUT_BYTES` while reading, with a truncation note.
- **Exit code 125** (docker could not start the container: missing image,
  unknown runtime, daemon down) is reported as `error`, not as a failing test.
- File operations act on the host working copy and refuse paths that escape
  it. `commit_and_push` raises `NotImplementedError`: verification never
  pushes.

### Threat model

Isolated from the tests:

- the worker's secrets and environment variables, and its files outside the
  copy (no host mount other than the copy);
- the network, unless `SANDBOX_NETWORK` opts into one;
- the host clone and later pipeline steps (copy-in);
- privilege escalation inside the container (no capabilities,
  `no-new-privileges`, non-root user, read-only root filesystem);
- resource exhaustion beyond the memory, CPU, pids, `/tmp` and time limits.

Not isolated:

- **Access to the Docker daemon is root-equivalent on the daemon's host.**
  The worker that holds the socket (or `DOCKER_HOST` credentials) can start a
  privileged container at will, so a compromised worker is a compromised
  daemon host. Do not mount the production host's socket into the worker.
  Prefer a **dedicated sandbox host** (a VM with nothing else on it) reached
  through `DOCKER_HOST`, **rootless Docker**, or both.
- **Kernel escapes.** With the default `runc` runtime the tests share the
  host kernel; a kernel vulnerability escapes the container. `SANDBOX_RUNTIME=runsc`
  (gVisor) puts a user-space kernel in between; a micro-VM backend
  (Firecracker) is stronger still.
- **What the tests print.** Output is untrusted and handled as described
  above; with a non-`none` network a test can also exfiltrate the repository
  contents (which it could read anyway) to whatever that network reaches.
- **Disk use** of the copy on the worker is not limited, and a test can fill
  the copy up to the free space of the worker's temp directory.
- **The change influencing its own tests** (see Limitations): the sandbox
  bounds what the tests can do to the host, not whether they are honest.

### Testing

`backend/test_docker_sandbox.py` mocks the docker CLI and checks the exact
argument list, the absence of host variables, the timeout kill, the output
cap and cleanup on errors. `backend/test_docker_sandbox_integration.py` runs
real containers and is skipped unless Docker can run Linux containers and
`DOCKER_SANDBOX_TESTS=1`; the `Sandbox Integration` workflow
(`.github/workflows/sandbox.yml`) runs it on `ubuntu-latest` whenever
`backend/runners/**` or `backend/verification.py` change: a passing, a
failing and a hanging repository, and one whose tests try the network and a
host environment variable.

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
- **The change can still influence its own tests.** Only `.automaintainer.yml`
  is protected. When the agent's target file is a test, `conftest.py`,
  `pytest.ini`, `pyproject.toml` (e.g. `addopts`), `setup.cfg` or
  `package.json` (the `test` script), the change can skip, rewrite or
  short-circuit the tests and make verification pass. Human review of such
  PRs, or required CI that the change cannot modify, remains necessary.
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
- **Test output does not reach the Maintainer prompt yet**; the Maintainer
  sees it only through the PR. Feeding it into the review is a follow-up of
  #411, as are change limits and audit rows.
- **Windows hosts** kill the process tree with `taskkill /T`; POSIX hosts use
  a new session and `killpg`.
