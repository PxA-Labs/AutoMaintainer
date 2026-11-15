# Agent quality evals

The backend test suite mocks every model call, so it checks plumbing, not
whether the agent writes correct code. The eval harness in `backend/evals/`
measures that. It runs the **Implementer** step on small fixture repositories
and checks the result with hidden tests. This is the MVP of epic #414.

What it measures: for a given issue text and target file, the prompt that
`agents.implementer_node` builds, the model's reply, and
`agents.normalize_generated_code` together produce a file that makes the
hidden acceptance tests pass without breaking the repository's own tests.

It does **not** measure the Architect, Visionary, PM or Maintainer, the
GitNexus tool loop, GitHub interaction, or multi-iteration repair. See
[Limitations](#limitations).

## Quick start

From `backend/`, with the backend dependencies installed:

```bash
# Check the harness itself. The oracle must score 100% and the no-op 0%.
python -m evals.run --mode oracle --min-resolve-rate 1.0 --out oracle.json
python -m evals.run --mode noop   --max-resolve-rate 0.0 --out noop.json

# Replay recorded model outputs (offline, deterministic, no API key).
python -m evals.run --mode replay --out replay.json

# Call the model. Needs GROQ_API_KEY. --record saves cassettes for later replay.
GROQ_API_KEY=... python -m evals.run --mode live --record my-cassettes --out live.json

# Safer: generate with the key, then score in a process without it (as CI does).
GROQ_API_KEY=... python -m evals.run --mode live --generate-only --record my-cassettes
python -m evals.run --mode replay --cassettes my-cassettes --fail-on-stale --out live.json

# Compare two runs task by task.
python -m evals.compare before.json after.json

# Unit tests for the harness (they also run in the normal backend suite).
python -m pytest -q evals
```

Other options: `--tasks a,b`, `--jobs N` (tasks evaluated in parallel),
`--timeout S` (limit per pytest run), `--model`, `--temperature` (default 0),
`--seed` (default 1234), `--cassettes DIR`, `--fail-on-stale` (replay:
exit 1 if any cassette answered a different prompt) and `--generate-only`
(live: only record cassettes into `--record DIR`; no candidate code runs and
the results file has `aggregate: null`).

### What CI runs, and subsets

- **Pull requests** that touch `backend/agents.py`, `backend/evals/**` or the
  lockfile run the offline job in `.github/workflows/evals.yml` on **all 30
  tasks**: the harness unit tests, the oracle (must be 100%), the no-op (must
  be 0%) and the synthetic replay (`--min-resolve-rate 0.6 --fail-on-stale`).
  Every task starts several fresh pytest subprocesses (test collection,
  repository tests, hidden tests), so with `--jobs 4` each mode takes a
  minute or two.
- **Live runs** are manual (`workflow_dispatch`); the `tasks` input takes
  `all` or a comma-separated subset, which also bounds the API cost.
- For a quick local check while editing the prompt or normalizer, run one
  task per category, then the full set before you push:

```bash
python -m evals.run --mode replay --tasks page_count,env_bool_override,retry_exception_filter,lru_eviction,version_compare,carrier_registry
```

A subset changes `taskset_hash`, so `evals.compare` only compares runs over
the same subset. Never quote a subset's rate as the set's rate.

Exit codes:

| Code | Meaning |
| --- | --- |
| 0 | Success |
| 1 | A `--min-resolve-rate` / `--max-resolve-rate` gate failed, or `--fail-on-stale` found stale cassettes |
| 2 | Harness or infrastructure error: missing key, missing cassette, or a failed LLM call |

## Modes and baselines

| Mode | Candidate file | Purpose |
| --- | --- | --- |
| `noop` | none (repository unchanged) | Hidden tests must fail and the repository's tests must pass. This catches vacuous or already-passing tests. |
| `oracle` | `tasks/<id>/solution/` | Must resolve 100%. This checks the tasks and the harness end to end. |
| `replay` | `cassettes/<set>/<id>.json` | Offline and deterministic. Used in CI. |
| `live` | the model, via `langchain_groq.ChatGroq` | The actual measurement. |

The oracle's reference file goes through `normalize_generated_code` like model
output does. So the oracle also shows that normalization accepts a correct
bare file.

## Task format

```
backend/evals/tasks/<task_id>/
  task.json      id, title, body, target_file, kind, category, difficulty,
                 provenance
  repo/          the fixture repository; the only thing the model may see
    tests/       pre-existing (pass-to-pass) tests, used to detect regressions
  hidden_tests/  acceptance (fail-to-pass) tests; never shown to the model
  solution/      reference version of target_file, used by the oracle
```

`kind`, `category` and `difficulty` come from closed lists; the loader
(`evals/taskset.py`) rejects any other value, so a typo cannot create a new
bucket:

| Field | Values |
| --- | --- |
| `kind` | `bugfix` (behaviour is wrong), `feature` (new behaviour or API), `refactor` (behaviour-preserving restructuring that the hidden tests can observe, such as a new public helper, parameter or registry, while the repository's own tests guard the old behaviour) |
| `category` | `logic` (wrong computation), `edge-case` (boundaries, empty or partial input), `input-validation` (rejecting bad input), `error-handling` (exceptions raised, propagated, retried or chained), `data-structure` (ordering, eviction, iteration, internal state), `api-change` (renames, new parameters, deprecations, extension points) |
| `difficulty` | `easy` (a local one- or two-line change), `medium` (several cases to get right), `hard` (an interplay of conditions or a new algorithm) |

The model receives the issue (`title` and `body`) as the Implementer's task
text and the current content of `target_file`. The prompt is identical to the
production one: `evals/tests/test_prompt_parity.py` runs the real
`implementer_node` with a mocked GitHub client and compares the prompts.

### Leakage rules

- **Never** put hidden tests, the solution, or anything derived from them into
  a prompt, an issue body, a cassette you share for prompt tuning, or a
  few-shot example. A task whose hidden tests the model has seen is no longer
  an evaluation task. Retire it and write a new one.
- Do not tune prompts against the hidden test output of a task and then report
  that task's score as unbiased. Keep a held-out split once the set is large
  enough (see epic #414).
- `test_taskset.py` checks that no hidden test name and no solution text
  appears in the rendered prompt.

### Leaky-task guards

A task is useless if a candidate can pass it without doing the work. Every
task is held to these rules, and `evals/tests/test_taskset.py` enforces the
ones that can be checked statically:

- **Behaviour, not files.** Hidden and repository tests call the code; they
  never read source or fixture files (`open(`, `read_text`, `inspect`,
  `__file__`, `Path(`, `os.path`, `glob`, `importlib`, `subprocess`,
  `exec(`/`eval(` are rejected). The candidate controls the target file, so a
  test that inspects it could be satisfied by a comment.
- **No trivial pass.** Every hidden test file imports the target module and
  holds at least two tests, with several inputs per behaviour (often
  parametrized), so no single hard-coded return value or stub passes. Hidden
  tests never compare against constants defined in the target module, which
  the candidate could change.
- **Fail before, pass after.** The no-op baseline must fail every task's
  hidden tests without a regression, and the oracle must pass all of them (CI
  runs both on every PR that touches the harness).
- **Fair.** Hidden tests check only what the issue states, including exact
  formats and exception types, so a correct reading of the issue passes.

### Provenance of the current set

The set has **30 tasks**. All of them were **written from scratch for this
harness in 2026-11**: the first 10 for the MVP, 20 more to narrow the
confidence intervals. None is taken or adapted from any public repository,
issue tracker or benchmark, such as SWE-bench, HumanEval or MBPP, so a model
cannot have memorized them from public data. They are small and synthetic,
so they measure basic single-file editing ability, not performance on
real-world issues.

| | Count |
| --- | --- |
| Kind | 19 bugfix, 6 feature, 5 refactor |
| Difficulty | 11 easy, 13 medium, 6 hard |
| Category | 7 edge-case, 6 input-validation, 6 data-structure, 5 logic, 4 api-change, 2 error-handling |

`error-handling` is the thinnest category; prefer it for the next tasks. The
results file carries each task's `kind` and `difficulty`; join on `task_id`
with `task.json` for `category`.

### Adding a task

1. Create `tasks/<new_id>/` with the layout above. `id` must equal the
   directory name. Write it yourself; do not copy public code, issues or
   benchmark problems. Set `kind`, `category` and `difficulty` from the lists
   above and `provenance` to a string starting with `original`.
2. The issue body must name the target file in a form `extract_target_file`
   understands, for example `` `pkg/module.py` ``, and should name no other
   file path. A test enforces this.
3. Describe the required behavior in the issue precisely enough that the
   hidden tests are fair. Hidden tests must check only what the issue states.
   Follow the [leaky-task guards](#leaky-task-guards).
4. Add at least one pre-existing test under `repo/tests/` that passes before
   and after the fix. For a refactor, these tests pin the old behaviour.
   Name hidden test files `test_hidden_<topic>.py`, so they never share a
   basename with a repository test.
5. Run `python -m evals.run --mode oracle --tasks <new_id>` (must resolve) and
   `--mode noop --tasks <new_id>` (must not resolve, with
   `regression: false`).
6. Replay needs a cassette for **every** task (a missing one is a harness
   error, exit 2). Hand-write `cassettes/synthetic/<new_id>.json` in the
   shape of the existing ones (`"synthetic": true`, `"model":
   "synthetic-fixture"`, a `note` that says what the response exercises),
   then run `python -m evals.rehash` to fill in `prompt_sha256` and
   `prompt_version`. Never paste a real model's output into the synthetic
   set.
7. Update the expected replay score in `evals/tests/test_harness.py` and, if
   the rate changes, the replay `--min-resolve-rate` gate in
   `.github/workflows/evals.yml` (the committed set is kept at exactly 60%,
   so adding correct and failing cassettes in a 3:2 ratio keeps the gate).
8. Run `python -m pytest -q evals` (this includes the full oracle, no-op and
   replay runs) and `black evals`, and update the counts in this document.

The backend suite never collects files under `tasks/`; `evals/conftest.py`
excludes them.

### How a pass is verified

pytest's exit code is not trusted on its own: candidate code that calls
`os._exit(0)` at import makes pytest exit 0 without running a test. Each run
writes a JUnit report into a fresh directory, and a run counts as passed only
if pytest exits 0, the report exists, and exactly as many tests passed as
`pytest --collect-only` finds on the reference solution (no failures, errors
or skips). The repository tests and the hidden tests run in two separate
fresh copies, so the hidden tests do not exist on disk while the candidate
runs the first time, and nothing that run writes (edited tests, a planted
`conftest.py` or `sitecustomize.py`) carries over.

The target file is resolved from the issue text with
`agents.extract_target_file`, as `implementer_node` does when no plan names
it. If that gives no path or a different path, the task is recorded as
`target_mismatch`: unresolved, not an error, and no model call is made.

## Reading results

`results.json` has three parts:

- `metadata`: `mode`, `prompt_version` (hash of the prompt template),
  `git_sha` and `git_dirty`, `taskset_hash` (content hash of the evaluated
  tasks), `task_ids`, `timeout_s`, Python and platform, start time. Live runs
  also record `model`, `temperature` and `seed`. Replay runs record the
  values stored in the cassettes and `synthetic_cassettes`.
- `aggregate`:
  - `n`, `resolved` and `resolve_rate`
  - `resolve_rate_ci95`: a 95% Wilson score interval
  - `output_rejected` and its rate
  - `regressions` and its rate
  - `target_mismatches`, `timed_out`, `errors` and `stale_cassettes`
  - when `errors > 0`, `resolve_rate` and `resolve_rate_ci95` are `null`:
    the run is incomplete, and neither counting errored tasks as failures nor
    dropping them gives an honest rate
  - `latency_ms_mean` and `latency_ms_median`
  - `tokens`: input, output and total, when the provider reports them
- `tasks`: one record per task, described below.

Fields of each task record:

| Field | Meaning |
| --- | --- |
| `resolved` | The hidden tests pass **and** the pre-existing tests still pass (fail-to-pass plus pass-to-pass). |
| `hidden_tests_passed` | The hidden tests alone pass. |
| `regression` | A pre-existing test failed after the change. `null` if the change was never applied. |
| `target_mismatch` / `target_resolved` | `extract_target_file` on the issue text did not return the task's `target_file`; `target_resolved` is what it returned. |
| `output_rejected` / `rejection_reason` | `normalize_generated_code` raised: refusal, truncation (`finish_reason=length`), ambiguous fences, empty output. Production aborts the run in this case. |
| `patch` | Unified diff of the applied change, for failure analysis. |
| `test_output` | Tail of the failing pytest output. |
| `stale_cassette` | The cassette was recorded for a different prompt. Its result does not describe the current prompt. |
| `error` | An infrastructure failure, such as a missing cassette or a network error. The run exits with code 2. Never report a run with errors. |

Report the interval with the rate, for example "18/30 resolved (60%, 95% CI
42–75%)". With n=30, the interval is roughly ±17 points around 50%; at the
MVP's n=10 it was about ±25 (6/10: 31–83%).

### About the committed cassettes

`cassettes/synthetic/` is **hand-written**. It is not output from a real model.
It covers every outcome the harness can classify:

- 18 correct answers in every reply shape the normalizer must accept: the
  bare file, a bare or `python`/`py`-tagged fence, a fence after a one-line
  lead-in or a chat-style explanation, and a fence followed by prose;
- 5 rejected replies: a refusal, a truncated reply (`finish_reason=length`),
  an empty reply, prose mixed with code lines before the fence (ambiguous),
  and an answer in the wrong language;
- 3 regressions: a renamed parameter, a removed public name, and a bare file
  with a syntax error;
- 4 plausible but wrong fixes that fail the hidden tests only.

Each cassette's `note` says which case it is. Replaying it measures the
**harness and the output normalizer**, not the model. CI gates on its known
score (18/30, `--min-resolve-rate 0.6`) and `evals/tests/test_harness.py`
pins each failure class above per task plus the 18/30 total, so a change to `normalize_generated_code` that
starts rejecting well-formed output, or accepting malformed output, fails the
PR. To track real model behavior offline, record a set with
`--mode live --record evals/cassettes/<name>` and replay it with
`--cassettes`.

Every cassette stores the hash of the prompt it answered. CI replays with
`--fail-on-stale`, so a PR that changes the Implementer prompt fails until the
cassettes are dealt with explicitly. For the synthetic set, run
`python -m evals.rehash`: its responses test the normalizer, not the model, so
re-binding them is correct. `evals.rehash` refuses real-model cassettes; those
must be re-recorded, because their answers were for the old prompt. A stale
synthetic set never tells you how the new prompt performs; only a live run
does.

## Determinism

Live mode calls the model with `temperature=0` and a fixed `seed`; both are
saved in the results. Groq treats `seed` as best effort, so outputs can still
vary between calls, for example after backend or model updates. Use replay
when you need byte-for-byte reproducibility. Hidden tests run with
`PYTHONHASHSEED=0` and with pytest plugin autoloading disabled. Results are
sorted by task id, so `--jobs` does not change them.

## Comparing two prompt or model versions fairly

1. Use the **same tasks**. `python -m evals.compare` refuses to compare when
   the `taskset_hash` values differ.
2. Change one thing at a time. The comparison lists any differences in mode,
   model, temperature, seed or timeout as `uncontrolled_settings`.
3. Compare **paired** outcomes, task by task, not two separate rates. Only the
   tasks where exactly one version succeeded carry information. `compare`
   lists them (`only_a_resolved` and `only_b_resolved`) and gives the exact
   McNemar p-value. Even 5 discordant tasks that all favor one side give
   only p=0.0625; it takes 6 of 6 (p=0.031) to reach p<0.05. Most differences
   you will see at n=30 are noise.
4. Overlapping Wilson intervals are not a valid test of a difference, because
   they ignore the pairing. Use the McNemar result.
5. Run live comparisons more than once (epic #414 asks for at least 3 seeds),
   and record real-model cassettes so a reviewer can replay exactly what you
   measured.
6. Read the failure `patch` and `test_output` fields, not only the totals. A
   higher score can come from tasks that are easier for one prompt by chance.

## Limitations

- **Small n, wide intervals.** 30 tasks give a 95% CI that is about 30 to 35
  points wide in the middle of the range (18/30: 42–75%). Use the harness to
  catch large regressions and to look at failures, not to rank close
  alternatives. Per-category counts (2 to 7 tasks) are far too small for
  per-category rates; read them as failure examples.
- **Single-file, small Python tasks.** The Implementer rewrites one whole
  file. The tasks contain no multi-file edits, no other languages, no large
  files, and no tasks where the model must find the right file.
- **Implementer only.** The plain completion here replaces production's
  GitNexus MCP tool loop (`run_llm_with_tools`), which also runs at the
  provider's default temperature. The planning agents, the Maintainer review
  loop and false-approve rates are not covered yet.
- **Synthetic tasks.** They were written for this harness, which keeps them
  free of contamination, but they are easier and cleaner than real issues.
  Epic #414 plans real-issue tasks with dated splits.
- **Not a sandbox.** Model-written code runs in a local `pytest` subprocess.
  The subprocess gets a minimal environment with no API keys and a timeout,
  but it keeps network access and file-system access and runs as the same
  user. A stripped environment does not hide secrets from it: on Linux it can
  read `/proc/<ppid>/environ` of the `evals.run` process, which holds
  `GROQ_API_KEY` in a one-step live run. Use `--generate-only` and score the
  recorded cassettes in a separate process without the key, as the workflow's
  live job does. Code that targets the harness (for example by forging the
  JUnit report it can locate) is also out of scope for these checks. Run live
  evals only where that is acceptable. The containerized sandbox runner is the
  planned replacement.
- **One sample per task.** There are no repeated seeds or bootstrap intervals
  yet.
- **Not production quality.** Live mode differs from production in three
  ways at once: no MCP tool loop, `temperature=0` instead of the provider
  default, and one sample per task. Read a live resolve rate as a regression
  signal for the prompt and normalizer, not as the Implementer's quality in
  production, and do not quote it without repeated seeds.
