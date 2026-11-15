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

# Compare two runs task by task.
python -m evals.compare before.json after.json

# Unit tests for the harness (they also run in the normal backend suite).
python -m pytest -q evals
```

Other options: `--tasks a,b`, `--jobs N` (tasks evaluated in parallel),
`--timeout S` (limit per pytest run), `--model`, `--temperature` (default 0),
`--seed` (default 1234) and `--cassettes DIR`.

Exit codes:

| Code | Meaning |
| --- | --- |
| 0 | Success |
| 1 | A `--min-resolve-rate` or `--max-resolve-rate` gate failed |
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
  task.json      id, title, body, target_file, kind, difficulty, provenance
  repo/          the fixture repository; the only thing the model may see
    tests/       pre-existing (pass-to-pass) tests, used to detect regressions
  hidden_tests/  acceptance (fail-to-pass) tests; never shown to the model
  solution/      reference version of target_file, used by the oracle
```

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

### Provenance of the current set

The 10 tasks (3 easy, 5 medium, 2 hard; 8 bug fixes and 2 small features)
were **written from scratch for this harness in 2026-11**. They are not taken
or adapted from any public repository, issue tracker or benchmark, such as
SWE-bench or HumanEval, so a model cannot have memorized them from public
data. They are small and synthetic, so they measure basic single-file editing
ability, not performance on real-world issues.

### Adding a task

1. Create `tasks/<new_id>/` with the layout above. `id` must equal the
   directory name. Write it yourself; do not copy public code or issues.
2. The issue body must name the target file in a form `extract_target_file`
   understands, for example `` `pkg/module.py` ``. A test enforces this.
3. Describe the required behavior in the issue precisely enough that the
   hidden tests are fair. Hidden tests must check only what the issue states.
4. Add at least one pre-existing test under `repo/tests/` that passes before
   and after the fix.
5. Run `python -m evals.run --mode oracle --tasks <new_id>` (must resolve) and
   `--mode noop` (must not resolve, with `regression: false`), then
   `python -m pytest -q evals` and `black evals`.
6. Add a cassette for replay, or regenerate the replay set, and update the
   replay `--min-resolve-rate` gate in `.github/workflows/evals.yml` if the
   expected count changes.

The backend suite never collects files under `tasks/`; `evals/conftest.py`
excludes them.

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
  - `timed_out`, `errors` and `stale_cassettes`
  - `latency_ms_mean` and `latency_ms_median`
  - `tokens`: input, output and total, when the provider reports them
- `tasks`: one record per task, described below.

Fields of each task record:

| Field | Meaning |
| --- | --- |
| `resolved` | The hidden tests pass **and** the pre-existing tests still pass (fail-to-pass plus pass-to-pass). |
| `hidden_tests_passed` | The hidden tests alone pass. |
| `regression` | A pre-existing test failed after the change. `null` if the change was never applied. |
| `output_rejected` / `rejection_reason` | `normalize_generated_code` raised: refusal, truncation (`finish_reason=length`), ambiguous fences, empty output. Production aborts the run in this case. |
| `patch` | Unified diff of the applied change, for failure analysis. |
| `test_output` | Tail of the failing pytest output. |
| `stale_cassette` | The cassette was recorded for a different prompt. Its result does not describe the current prompt. |
| `error` | An infrastructure failure, such as a missing cassette or a network error. The run exits with code 2. Never report a run with errors. |

Report the interval with the rate, for example "6/10 resolved (60%, 95% CI
31–83%)". With n=10, the interval is roughly ±25 points.

### About the committed cassettes

`cassettes/synthetic/` is **hand-written**. It is not output from a real model.
It covers every outcome the harness can classify: bare-file and fenced
correct answers, a partial fix, a refusal, a truncated reply, and a change
that breaks an existing caller. Replaying it measures the **harness and the
output normalizer**, not the model. CI gates on its known score (6/10), so a
change to `normalize_generated_code` that starts rejecting well-formed output
fails the PR. To track real model behavior offline, record a set with
`--mode live --record evals/cassettes/<name>` and replay it with
`--cassettes`.

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
   McNemar p-value. With 10 tasks, even 4 discordant tasks that all favor one
   side give p=0.125. Most differences you will see at this size are noise.
4. Overlapping Wilson intervals are not a valid test of a difference, because
   they ignore the pairing. Use the McNemar result.
5. Run live comparisons more than once (epic #414 asks for at least 3 seeds),
   and record real-model cassettes so a reviewer can replay exactly what you
   measured.
6. Read the failure `patch` and `test_output` fields, not only the totals. A
   higher score can come from tasks that are easier for one prompt by chance.

## Limitations

- **Small n, wide intervals.** 10 tasks give a 95% CI that is about 50 points
  wide. Use the harness to catch large regressions and to look at failures,
  not to rank close alternatives.
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
  but it keeps network access and file-system access. Run live evals only
  where that is acceptable. The containerized sandbox runner is the planned
  replacement.
- **One sample per task.** There are no repeated seeds or bootstrap intervals
  yet.
