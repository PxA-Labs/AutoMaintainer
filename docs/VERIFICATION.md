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
agents._verify_and_report  (one call per iteration)
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
| `error`   | No trustworthy answer: timeout, refused in production, invalid config, clone failed, branch moved, sandbox failure. | `error` |
| `skipped` | Nothing ran: no tests detected, or `VERIFY_CHANGES=false`.              | `error`       |

### Merge gating

`MERGE_POLICY=ci` merges only when every commit status and check run on the PR
head is green (`agents._ci_checks_passed`). Verification posts its result as a
commit status on that head, and only `passed` maps to `success`, so `ci` merges
only a head whose verification passed. `error` and `skipped` fail closed.
Because the status is bound to a SHA, a later push to the branch is never
merged on an older result.

If the status cannot be posted while `MERGE_POLICY=ci`, the run stops with
`VERIFICATION_STATUS_FAILED`; the PR stays open. Under `human` (the default)
and `auto`, a failure to post is logged and the run continues.

For a backstop that does not depend on the agent at all, mark
`automaintainer/verification` as a **required status check** in the branch
protection of maintained repositories. GitHub then refuses any merge without a
green verification, including in the edge case below (a resumed run).

The GitHub App needs the **Commit statuses: read & write** repository
permission.

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
  so a change cannot choose the command that judges it.

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
| `VERIFY_ALLOW_LOCAL` | unset | With `ENVIRONMENT=production`, `true` allows the local runner anyway. Leave it unset. |
| `VERIFY_TIMEOUT_SECONDS` | `300` | Default wall-clock limit for the test command. |
| `VERIFY_MAX_TIMEOUT_SECONDS` | `1800` | Upper bound for a repository's `timeout_seconds`. |
| `VERIFY_MAX_OUTPUT_BYTES` | `65536` | Output kept (the tail). The PR body shows the last 3000 characters. |
| `VERIFY_PYTHON` | `python` | Interpreter used by the detected pytest command. |

## Security model

The test command executes code from the target repository, including code the
agent just wrote from an untrusted issue. Treat it as hostile.

- **Production requires a sandbox.** With `ENVIRONMENT=production` and no
  `SANDBOX_BACKEND`, verification refuses to run (`error`, so `ci` does not
  merge) unless `VERIFY_ALLOW_LOCAL=true`. The refusal happens before the
  repository is cloned.
- **The local runner is defense in depth, not isolation.** It runs an
  argument list (no shell) in the temporary clone, with stdin closed, an
  environment reduced to `PATH` and a few OS variables (no API keys, database
  URLs or tokens), `HOME`/`TMP` pointed at an empty scratch directory, a
  wall-clock timeout that kills the whole process tree, and an output cap. It
  does **not** stop the tests from reading files the server user can read,
  using the network, or consuming CPU and memory up to the timeout. Use it for
  development only.
- **A sandbox backend** should run the command in an ephemeral container or
  micro-VM with no secrets, network egress denied (or limited to package
  mirrors) and CPU, memory and time limits. Those properties are the backend's
  responsibility; this repository only defines the interface
  (`runners/sandbox.py`) and does not ship a backend yet.
- **Output is untrusted.** In the PR body it is placed in a code fence longer
  than any backtick run it contains, so it cannot inject Markdown or
  `@mentions`.

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
- **Resumed runs.** If a run crashes after pushing but before the status is
  posted, the resumed run reuses the pushed commit without re-verifying. With
  `MERGE_POLICY=ci` the gate then sees no verification status and relies on
  the other checks; the required-status-check setting above closes this gap.
- **Verification can make `ci` merge in a repository with no other CI.**
  `_ci_checks_passed` requires at least one successful check; a passed
  verification is one. Repositories that want an independent CI signal as
  well should make their CI job a required check in branch protection.
- **Test output does not reach the Maintainer prompt yet**; the Maintainer
  sees it only through the PR. Feeding it into the review is a follow-up of
  #411, as are the Docker/gVisor backend, change limits and audit rows.
- **Windows hosts** kill the process tree with `taskkill /T`; POSIX hosts use
  a new session and `killpg`.
