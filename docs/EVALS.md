# Agent quality evaluation

The backend test suite mocks every model call. It checks plumbing, not whether
the agents produce correct changes. The evaluation harnesses in
`backend/evals/` measure that.

| Harness | Command | What it runs | Measures |
| --- | --- | --- | --- |
| [End-to-end](#end-to-end-evaluation-whole-engine) | `python -m evals.e2e.run` | The whole `AutoMaintainerEngine` on 24 tasks, plus the Maintainer step alone on 27 Reviewer cases | Resolve rate, PRs merged without help, fix iterations, false approvals and rejections, tokens, cost, latency |
| [Implementer](#implementer-harness) | `python -m evals.run` | The Implementer prompt and output normalizer on 30 single-file tasks | Resolve rate of one Implementer call |

The end-to-end evaluation is the baseline that the ADK Reviewer (#525) and
code retrieval (#526, [RAG.md](RAG.md)) are measured against. The Implementer
harness is older and narrower; it remains in CI.

**Current status.** Only offline modes have run. The committed cassettes are
synthetic: they test the harnesses, not a model. No real-model result is
committed. The first live baseline is an owner step
([Running the live baseline](#running-the-live-baseline)).

All commands below run from `backend/` with the backend dependencies
installed (`uv sync --frozen --group dev --extra server`).

## End-to-end evaluation (whole engine)

`backend/evals/e2e/` runs a targeted run on each task's issue through the real
LangGraph graph (Architect, Visionary, PM, Implementer, Maintainer and the
retry loop), the real verification step, change limits and merge policy. On a
targeted run the Architect and Visionary nodes take the issue as given and
make no model call, so the PM, Implementer and Maintainer are the agents whose
model behaviour is measured.

Three dependencies are replaced, all through the engine's injection points
(`AutoMaintainerEngine(supabase=, llm=, github=, emitter=, nodes=)`). The
engine's runtime code is not modified.

- **GitHub:** `fakes.GitRepository`, a bare git repository that holds only the
  task's repository snapshot, behind the subset of the PyGithub API the nodes
  call. Every agent write is a real commit with a fixed author and dates, so
  SHAs are reproducible. The clone URL is a `file:` URL, so verification
  clones and tests the pushed head as it would on GitHub.
- **Supabase:** `fakes.InMemorySupabase` (run row, checkpoints, audit rows).
- **The model:** an injected transport. `oracle` and `noop` are scripted
  agents, `replay` answers from cassettes and `live` calls Groq.

The engine configuration (`evals/e2e/runner.py`, `make_config`):
`merge_policy="human"`, three iterations, the default change limits,
verification on with the local runner (`runner_type="local"`) and
`reviewer_backend="langgraph"` unless `--reviewer-backend` says otherwise.
Code retrieval is not enabled. A repository writer starts every run.

### Quick start

```bash
python -m pytest -q evals/e2e        # data validation and unit tests
python -m evals.e2e.run --mode oracle --out oracle.json --min-resolve-rate 1.0
python -m evals.e2e.run --mode noop   --out noop.json   --max-resolve-rate 0
python -m evals.e2e.run --mode replay --out replay.json --fail-on-stale
```

Each run writes a JSON report (`--out`, default `e2e-results.json`) and a
Markdown summary (`--markdown`, default `--out` with `.md`). Live mode is
described in [Running the live baseline](#running-the-live-baseline).

### Options

| Option | Default | Meaning |
| --- | --- | --- |
| `--mode` | required | `oracle`, `noop`, `replay` or `live` ([Modes](#modes)). |
| `--suite` | `all` | `pipeline`, `reviewer` or `all`. |
| `--tasks`, `--cases` | `all` | Comma-separated task or Reviewer case ids. |
| `--repeats` | 3 in `live`, 1 in `oracle` and `noop` | Runs per task and case. `replay` always runs the number of repeats the cassettes recorded. It refuses a different `--repeats` and cassettes that recorded different numbers of runs, so no recording is counted twice or partly dropped. |
| `--jobs` | 2 | Runs in flight at once. |
| `--cassettes` | `evals/e2e/cassettes/fixtures` | Directory replayed in `replay` mode. |
| `--record` | none | Save cassettes of this run into a directory. |
| `--model` | `llama-3.3-70b-versatile` in `live` | Model for every role. |
| `--temperature`, `--seed` | 0, 1234 | Sampling settings for `live`. |
| `--requests-per-minute`, `--tokens-per-minute` | 30, 6000 in `live` | The engine's client-side rate budget. |
| `--prices` | built-in table | JSON price table ([Price table](#price-table-estimate)). |
| `--timeout` | 120 | Seconds for each hidden-test scoring run in the `pipeline` suite. The `reviewer` suite ignores it. The engine's own verification inside a run uses `AutoMaintainerConfig.timeout_seconds` (300), not this value. |
| `--reviewer-backend` | `langgraph` | `langgraph` or `adk`; `adk` needs `--mode live` ([Comparing Reviewer backends](#comparing-reviewer-backends-525)). |
| `--fail-on-stale` | off | Exit 1 if any replayed call's prompt changed since recording. |

Gates (each exits 1 when it fails): `--min-resolve-rate`,
`--max-resolve-rate`, `--min-merged-rate`, `--max-false-approval-rate`,
`--min-false-approval-rate`, `--max-false-rejection-rate`.

A gate on a suite that did not run is skipped silently: for example,
`--suite pipeline --max-false-approval-rate 0` exits 0 because there is no
Reviewer summary to check. Run `--suite all`, or the suite the gate belongs
to, when you rely on a gate.

Exit codes: 0 success; 1 a gate failed or `--fail-on-stale` found a stale
call; 2 a harness error (missing key or cassette, or a run that could not be
scored). Any harness error voids the rates.

### Task set

24 tasks, each in `evals/e2e/tasks/<id>/`. The layout is that of the
Implementer set (`task.json`, `repo/`, `hidden_tests/`, `solution/`, see
[Task format](#task-format)), and `task.json` adds `source`, `defect` and
`harness_files`. Each task is one upstream file at a pinned commit, taken
from ten permissively licensed projects. The table reflects the `source`
records in `task.json`:

| Project | License | Commit | Tasks |
| --- | --- | --- | --- |
| [boltons](https://github.com/mahmoud/boltons) | BSD-3-Clause | `4332b35a` | 6 |
| [inflection](https://github.com/jpvanhal/inflection) | MIT | `b00d4d34` | 3 |
| [cachetools](https://github.com/tkem/cachetools) | MIT | `498bd83c` | 2 |
| [hashids-python](https://github.com/davidaurelio/hashids-python) | MIT | `138a12e8` | 2 |
| [isoweek](https://github.com/gisle/isoweek) | BSD-2-Clause | `4848cc22` | 2 |
| [pytimeparse](https://github.com/wroberts/pytimeparse) | MIT | `6d26b2a9` | 2 |
| [python-json-pointer](https://github.com/stefankoegl/python-json-pointer) | BSD-3-Clause | `54039218` | 2 |
| [toolz](https://github.com/pytoolz/toolz) | BSD-3-Clause | `451af60d` | 2 |
| [xmltodict](https://github.com/martinblech/xmltodict) | MIT | `8d7f1fdb` | 2 |
| [shortuuid](https://github.com/skorokithakis/shortuuid) | BSD-3-Clause | `16374d28` | 1 |

How each task was built:

- **Oracle fix** (`solution/`): the upstream file, reformatted with black so
  the repository's `black --check` gate passes (layout and quotes only).
- **Snapshot** (`repo/`): the same file with one injected regression
  (described in `task.json` `defect`), the upstream `LICENSE`, a small visible
  test file written for the evaluation and, where the upstream package
  `__init__.py` imports modules the snapshot leaves out, an empty package
  marker. These additions are listed in `harness_files`.
- **Issue:** written as a bug report (symptom, reproduction, expected
  behaviour). It names the target file, which targeted runs require.
- **Hidden tests:** written for the evaluation; they check only what the
  issue states.

These are real code with synthetic regressions, not historical upstream
issues. The bugs and fixes are known exactly, but a model may have seen the
fixed upstream code during training, which makes the tasks easier than
unseen code. Treat the scores as a regression baseline for this pipeline, not
as a benchmark result.

`evals/e2e/tests/test_e2e_data.py` checks, for every task, that the oracle
passes the hidden tests, that the snapshot fails them without breaking its
own tests, and that the hidden tests, the fix and `task.json` are absent from
the git object store the agents and the verification clone see and
unreachable through the fake API (including by path traversal).

### Reviewer change set

`evals/e2e/reviewer/` holds 27 cases, each a `case.json` and the changed
files:

- **14 defective changes**, by defect class: `wrong-logic` 4,
  `broken-api` 2, `deleted-test` 2, `injected-instruction` 2 (an instruction
  to the reviewer in a comment), `missing-edge-case` 2,
  `special-cased-values` 1, `swallowed-exception` 1. The hidden tests catch
  every one of them (checked by the data validation tests).
- **13 correct changes**: the oracle fixes of the same tasks.

For each case the harness opens a PR with the change (replacement `pm` and
`implementer` nodes), verifies it with the engine's own verification step,
and the real `maintainer` node reviews it with that result in its prompt
(`max_iterations=1`).

### Modes

| Mode | Agents | Purpose |
| --- | --- | --- |
| `oracle` | Scripted: approve and write the reference fix; the Reviewer always gives the right verdict. | Must resolve every task and judge every case correctly. Checks the tasks and the harness. |
| `noop` | Scripted: the Implementer returns the file unchanged; the Reviewer approves everything. | Must resolve nothing and approve every defective change. Catches tasks that pass without a fix. |
| `replay` | Answers from cassettes (default: the committed `cassettes/fixtures`). | Offline and deterministic. Used in CI. |
| `live` | Groq, with the keys in `GROQ_API_KEY` and `GROQ_API_KEY_1` to `GROQ_API_KEY_19`. | The actual measurement. Manual only. |

### Metrics

Per run (`pipeline.runs` in the JSON report):

| Field | Meaning |
| --- | --- |
| `attempted` | The PM approved and the Implementer ran at least once. |
| `pr_opened` | The run opened a pull request. |
| `resolved` | The hidden tests pass and the repository's tests still pass on the final PR head (the newest PR). |
| `maintainer_approved` | The last Maintainer verdict approved. Under `human` the PR then waits for a person. |
| `merged_without_help` | `maintainer_approved` and `resolved`: the PR would have merged correctly with no human change. |
| `fix_iterations` | Implementer iterations, 1 to 3. |
| `prompt_tokens`, `completion_tokens` | From the provider's usage (live, or cassettes recorded live); `null` for scripted agents. |
| `cost_usd` | Tokens priced with the price table; `null` when usage or price is unknown. |
| `latency_s` | Wall-clock time of the engine run, scoring excluded. |
| `stale_calls` | Replayed calls whose prompt changed since recording. |

Aggregation:

- **Per task:** rates over its repeats, and mean and standard deviation of
  iterations, tokens, cost and latency.
- **Overall:** each metric is computed per repeat across all tasks, then
  reported as mean and standard deviation over the repeats.
  `resolved_ci95` is a 95% Wilson interval over all runs pooled. Repeats of
  one task are correlated, so this interval is too narrow when they agree.
- **Reviewer:** `false_approval_rate` is defective changes approved divided by
  defective changes. `false_rejection_rate` is correct changes rejected
  divided by correct changes. Both are computed per repeat, then reported as
  mean and standard deviation, with approvals per defect class.

### Price table (estimate)

`evals/e2e/pricing.py` holds a built-in table in USD per million tokens:

| Model | Input | Output |
| --- | --- | --- |
| `llama-3.3-70b-versatile` | 0.59 | 0.79 |
| `llama-3.1-8b-instant` | 0.05 | 0.08 <!-- gitleaks:allow --> |

These are Groq's earlier public list prices. They could not be confirmed
when the table was written, so treat every cost in a report as an estimate.
The report records the price source. Pass `--prices FILE` with current
prices before quoting a cost.

### Cassettes and the fixture baseline

A cassette (`cassettes/<set>/<pipeline|reviewer>/<id>.json`) stores, per
repeat, every model call: role, answer, usage, latency and the hash of the
normalised prompt. Normalisation drops the verification output block (host
test output), timings, host paths and SHAs. A replay on another machine is
therefore not stale, while any change to what the agents are asked is.

The committed set `cassettes/fixtures/` is synthetic. `evals/e2e/make_fixtures.py`
records it from the scripted oracle and no-op agents and mixes them by a fixed
rule: every third task by sorted id replays the no-op, and every fourth
defective change replays the rubber-stamp Reviewer. A full replay must
therefore score:

| Suite | Fixture baseline |
| --- | --- |
| Pipeline | 16/24 resolved (0.667), 16/24 merged without help |
| Reviewer | 3/14 false approvals (0.214), 0/13 false rejections |

`evals/e2e/tests/test_e2e_fixtures.py` pins these two rates, and the CI gates
below enforce them. They describe the harness and the engine plumbing, never
a model. After a prompt change, regenerate the set with
`python -m evals.e2e.make_fixtures` and check that only hashes changed.

### CI gates

`.github/workflows/evals.yml` runs the `e2e-offline` job on pull requests to
`main` that touch `backend/automaintainer/_runtime/agents.py`,
`backend/agents.py`, `backend/evals/**`, `backend/pyproject.toml`,
`backend/uv.lock` or the workflow itself. It installs
`--extra server --group dev` and runs:

| Step | Command and gates |
| --- | --- |
| Data validation and unit tests | `python -m pytest -q evals/e2e` |
| Oracle | `--mode oracle`: `--min-resolve-rate 1.0 --min-merged-rate 1.0 --max-false-approval-rate 0 --max-false-rejection-rate 0` |
| No-op | `--mode noop`: `--max-resolve-rate 0 --min-false-approval-rate 1.0 --max-false-rejection-rate 0` |
| Fixture replay | `--mode replay --fail-on-stale`: `--min-resolve-rate 0.66 --max-resolve-rate 0.67 --min-false-approval-rate 0.21 --max-false-approval-rate 0.22 --max-false-rejection-rate 0` |

The replay bands bracket the fixture baseline (16/24 = 0.667 and
3/14 = 0.214). Results are uploaded as the `e2e-results-offline` artifact.
Changes to engine files outside the listed paths do not trigger the job.

### Running the live baseline

Live mode calls Groq and costs money, so only the owner runs it. Read the
cautions below first.

**In CI.** Dispatch `.github/workflows/evals.yml` manually with `suite` set to
`e2e` (or `both`; the default `implementer` runs only the Implementer live
job). The inputs are `e2e_tasks` and `e2e_cases` (default `all`),
`e2e_repeats` (default 3) and `model` (default `llama-3.3-70b-versatile`). The
`e2e-live` job first runs the oracle on the same subset as a sanity check,
then runs live with `--jobs 1`, records cassettes, and uploads the
`e2e-results-live` artifact. The job uses the `GROQ_API_KEY` repository
secret.

**Locally:**

```bash
GROQ_API_KEY=... python -m evals.e2e.run --mode live --record my-cassettes --out live.json
```

To replay a recording, from the artifact or a local run:

```bash
python -m evals.e2e.run --mode replay --cassettes evals/e2e/cassettes/<name> --fail-on-stale
```

A full run at the default 3 repeats is 72 pipeline runs and 81 Reviewer runs.
Use `--tasks` and `--cases` to bound cost.

Cautions:

- **Model-written code runs in the process that holds the key.**
  Verification and the hidden tests execute the PR head. The child processes
  get a scrubbed environment, but on Linux they can read the parent's
  environment (`/proc/<ppid>/environ`). Use a dedicated, low-limit evaluation
  key, run only where that exposure is acceptable, and rotate the key after a
  run. Unlike the Implementer harness, generation and scoring cannot be split
  into two processes, because the pipeline interleaves model calls with test
  runs.
- **Redaction is partial.** Every `GROQ_API_KEY*` value is replaced with
  `[REDACTED]` in the JSON and Markdown reports, the printed summary and
  recorded cassettes. A key that model code encodes or splits before printing
  is not caught.
- **Same-host leakage.** The leak guards keep the hidden tests and the fix
  out of the fake GitHub API and the clone the agents see. Model-written code
  still runs on the host and could read `evals/e2e/tasks/*/solution/`,
  `hidden_tests/` or the committed cassettes (whose Implementer answers hold
  the oracle fix) by absolute path. That takes deliberately adversarial code,
  but a resolved task in live mode is not proof that the model never saw the
  answer. Scoring needs the hidden tests, so the harness does not isolate
  them. Check resolved diffs by hand before quoting a live number.
- **Not production behaviour.** Live mode uses plain completions through the
  injected transport at a fixed temperature (0) and seed. Production's
  GitNexus tool loop and its silent 8B fallback are not exercised, because
  they cannot report per-call usage.

### Comparing Reviewer backends (#525)

`--reviewer-backend langgraph|adk` selects who reviews in the maintainer
node: the single-shot prompt (`langgraph`, the default and the deployment
default of `REVIEWER_BACKEND`) or the ADK Reviewer (`adk`, see the
[design note](design/adk-specialist-agents.md)). The report records the
backend under `metadata.engine.reviewer_backend`. `adk` is accepted only in
`--mode live`: the ADK Reviewer calls its model itself, not through the
harness transport, so there is nothing to replay, and offline it would reach
a provider with the offline key. The `evals.yml` workflow does not pass this
option, so the comparison runs locally.

Owner procedure:

1. Run the tool-calling spike (`python -m evals.spike_adk_groq`, see the
   design note) and record a GO.
2. Run both backends with a dedicated evaluation key and the same model:

   ```bash
   uv sync --frozen --group dev --extra server --extra adk
   export GROQ_API_KEY=...   # read the live-mode cautions first
   for backend in langgraph adk; do
     uv run --frozen python -m evals.e2e.run --mode live --suite reviewer \
       --repeats 3 --reviewer-backend "$backend" \
       --out "e2e-reviewer-$backend.json"
   done
   ```

3. Read `reviewer.summary.overall` in each report:
   - `false_approval_rate`: mean and standard deviation over the repeats,
     each over the 14 defective cases. This figure decides. Compare it with
     the `langgraph` live run of the same model, day and key, and with the
     fixture baseline of 3/14 (0.214). The fixture figure is synthetic; it is
     the harness's reference, not a model's.
   - `false_rejection_rate` over the 13 correct cases.
   - `latency_s`: mean and standard deviation pooled over every run of
     every repeat, not computed per repeat. For fix iterations, run
     `--suite pipeline` with each backend and compare `fix_iterations`.
   - Cost: the token and cost columns count only calls made through the
     harness transport. With `adk` they leave out the Reviewer's own calls
     (its runs show `llm_calls` 0). Take those tokens from the provider's
     usage page for the evaluation key. For a Gemini fallback, pass
     `--prices` with its prices. `cost_usd` is one total over all repeats
     (its `std` is 0), not a mean per repeat; divide by `repeats` for a
     per-repeat figure.

With 14 defective cases, one approval moves the rate by 0.071, so treat a
difference smaller than the two spreads as no difference. The decision rule
from the design note: enable `adk` only if its false-approval mean is not
worse than the `langgraph` live mean and at least one other figure improves.
Otherwise keep `REVIEWER_BACKEND=langgraph` and record the numbers in #525.

### Limitations of the end-to-end evaluation

- **Synthetic regressions in real code** that a model may have seen in
  training; single-file fixes; small libraries; Python only.
- **Small n.** 24 tasks and 27 Reviewer cases give wide intervals. Compare
  versions task by task, as described in
  [Comparing two versions fairly](#comparing-two-versions-fairly).
- **Offline GitHub.** No other CI checks, permissions or webhooks. Only
  targeted runs and `MERGE_POLICY=human` are covered.
- **Not a sandbox.** Verification runs the snapshot's tests with the local
  runner on the host. Running live evaluations inside a sandbox backend is
  still open (#532).
- **The Reviewer suite starts from a PR the harness opens,** so it measures
  the Maintainer step in isolation. The pipeline suite includes it in
  context.
- **Not yet built:** a script that rebuilds the task set from upstream
  (`build_tasks.py`). `task.json` records each defect, upstream commit and
  path.

## Implementer harness

The Implementer harness is the MVP of epic #414. It runs the Implementer
step on small fixture repositories and checks the result with hidden tests.
For a given issue text and target file, it measures whether the prompt that
`agents.implementer_node` builds, the model's reply and
`agents.normalize_generated_code` together produce a file that makes the
hidden acceptance tests pass without breaking the repository's own tests.

It does not measure the Architect, Visionary, PM or Maintainer, the GitNexus
tool loop, GitHub interaction or multi-iteration repair. The
[end-to-end evaluation](#end-to-end-evaluation-whole-engine) covers those.

### Quick start

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
python -m pytest -q evals --ignore=evals/e2e
```

Other options: `--tasks a,b`, `--tasks-dir DIR` (default
`backend/evals/tasks`), `--jobs N` (tasks evaluated in parallel,
default 1), `--timeout S` (seconds per pytest run, default 60), `--model`
(default `llama-3.3-70b-versatile`), `--temperature` (default 0), `--seed`
(default 1234), `--cassettes DIR`, `--fail-on-stale` (replay: exit 1 if any
cassette answered a different prompt) and `--generate-only` (live: only record
cassettes into `--record DIR`; no candidate code runs and the results file has
`aggregate: null`).

Exit codes:

| Code | Meaning |
| --- | --- |
| 0 | Success |
| 1 | A `--min-resolve-rate` or `--max-resolve-rate` gate failed, or `--fail-on-stale` found stale cassettes |
| 2 | Harness or infrastructure error: missing key, missing cassette, or a failed LLM call |

### CI

The `offline` job of `.github/workflows/evals.yml` runs on the same pull
request paths as the end-to-end job, on all 30 tasks:

| Step | Command and gate |
| --- | --- |
| Harness unit tests | `python -m pytest -q evals --ignore=evals/e2e` |
| Oracle | `--mode oracle --min-resolve-rate 1.0` |
| No-op | `--mode noop --max-resolve-rate 0.0` |
| Cassette replay | `--mode replay --min-resolve-rate 0.6 --fail-on-stale` |

Each mode runs with `--jobs 4`. Every task starts several fresh pytest
subprocesses (test collection, repository tests, hidden tests).

Live runs are manual: dispatch the workflow with `suite` set to
`implementer` (the default) or `both`. The `tasks` input takes `all` or a
comma-separated subset, which also bounds the API cost. The `live` job runs
the oracle, then generates in a step that holds `GROQ_API_KEY` and runs no
tests (`--generate-only`), then scores the recorded cassettes in a separate
step without the secret.

For a quick local check while editing the prompt or normalizer, run one task
per category, then the full set before you push:

```bash
python -m evals.run --mode replay --tasks page_count,env_bool_override,retry_exception_filter,lru_eviction,version_compare,carrier_registry
```

A subset changes `taskset_hash`, so `evals.compare` only compares runs over
the same subset. Do not quote a subset's rate as the set's rate.

### Modes

| Mode | Candidate file | Purpose |
| --- | --- | --- |
| `noop` | none (repository unchanged) | Hidden tests must fail and the repository's tests must pass. This catches vacuous or already-passing tests. |
| `oracle` | `tasks/<id>/solution/` | Must resolve 100%. This checks the tasks and the harness end to end. |
| `replay` | `cassettes/<set>/<id>.json` | Offline and deterministic. Used in CI. |
| `live` | the model, through `langchain_groq.ChatGroq` | The actual measurement. |

The oracle's reference file goes through `normalize_generated_code` like
model output does, so the oracle also shows that normalization accepts a
correct bare file.

### Task format

```
backend/evals/tasks/<task_id>/
  task.json      id, title, body, target_file, kind, category, difficulty,
                 provenance
  repo/          the fixture repository; the only thing the model may see
    tests/       pre-existing (pass-to-pass) tests, used to detect regressions
  hidden_tests/  acceptance (fail-to-pass) tests; never shown to the model
  solution/      reference version of target_file, used by the oracle
```

`kind`, `category` and `difficulty` come from closed lists. The loader
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

#### Leakage rules

- Never put hidden tests, the solution or anything derived from them into a
  prompt, an issue body, a cassette you share for prompt tuning, or a
  few-shot example. A task whose hidden tests the model has seen is no longer
  an evaluation task. Retire it and write a new one.
- Do not tune prompts against the hidden test output of a task and then
  report that task's score as unbiased. Keep a held-out split once the set is
  large enough (see epic #414).
- `test_taskset.py` checks that no hidden test name and no solution text
  appears in the rendered prompt.

#### Leaky-task guards

A task is useless if a candidate can pass it without doing the work. Every
task follows these rules, and `evals/tests/test_taskset.py` enforces the ones
that can be checked statically:

- **Behaviour, not files.** Hidden and repository tests call the code; they
  never read source or fixture files (`open(`, `read_text`, `inspect`,
  `__file__`, `Path(`, `os.path`, `glob`, `importlib`, `subprocess`,
  `exec(` and `eval(` are rejected). The candidate controls the target file,
  so a test that inspects it could be satisfied by a comment.
- **No trivial pass.** Every hidden test file imports the target module and
  holds at least two tests, with several inputs per behaviour (often
  parametrized), so no single hard-coded return value or stub passes. Hidden
  tests never compare against constants defined in the target module, which
  the candidate could change.
- **Fail before, pass after.** The no-op baseline must fail every task's
  hidden tests without a regression, and the oracle must pass all of them.
- **Fair.** Hidden tests check only what the issue states, including exact
  formats and exception types, so a correct reading of the issue passes.

#### Provenance of the task set

The set has 30 tasks. All of them were written from scratch for this harness
in 2026-11: the first 10 for the MVP and 20 more to narrow the confidence
intervals. None is taken or adapted from a public repository, issue tracker
or benchmark such as SWE-bench, HumanEval or MBPP, so a model cannot have
memorized them from public data. They are small and synthetic, so they
measure basic single-file editing, not performance on real-world issues.

| | Count |
| --- | --- |
| Kind | 19 bugfix, 6 feature, 5 refactor |
| Difficulty | 11 easy, 13 medium, 6 hard |
| Category | 7 edge-case, 6 input-validation, 6 data-structure, 5 logic, 4 api-change, 2 error-handling |

`error-handling` is the thinnest category; prefer it for new tasks. The
results file carries each task's `kind` and `difficulty`; join on `task_id`
with `task.json` for `category`.

#### Adding a task

1. Create `tasks/<new_id>/` with the layout above. `id` must equal the
   directory name. Write the task yourself; do not copy public code, issues
   or benchmark problems. Set `kind`, `category` and `difficulty` from the
   lists above and `provenance` to a string starting with `original`.
2. The issue body must name the target file in a form `extract_target_file`
   understands, for example `` `pkg/module.py` ``, and should name no other
   file path. A test enforces this.
3. Describe the required behaviour precisely enough that the hidden tests are
   fair. Hidden tests check only what the issue states. Follow the
   [leaky-task guards](#leaky-task-guards).
4. Add at least one pre-existing test under `repo/tests/` that passes before
   and after the fix. For a refactor, these tests pin the old behaviour. Name
   hidden test files `test_hidden_<topic>.py`, so they never share a basename
   with a repository test.
5. Run `python -m evals.run --mode oracle --tasks <new_id>` (must resolve) and
   `--mode noop --tasks <new_id>` (must not resolve, with
   `regression: false`).
6. Replay needs a cassette for every task; a missing one is a harness error
   (exit 2). Hand-write `cassettes/synthetic/<new_id>.json` in the shape of
   the existing ones (`"synthetic": true`, `"model": "synthetic-fixture"`, a
   `note` that says what the response exercises), then run
   `python -m evals.rehash` to fill in `prompt_sha256` and `prompt_version`.
   Never paste a real model's output into the synthetic set.
7. Update the expected replay score in `evals/tests/test_harness.py` and, if
   the rate changes, the replay `--min-resolve-rate` gate in
   `.github/workflows/evals.yml`. The committed set is kept at exactly 60%,
   so adding correct and failing cassettes in a 3:2 ratio keeps the gate.
8. Run `python -m pytest -q evals --ignore=evals/e2e` (this includes the full
   oracle, no-op and replay runs) and `black evals`, and update the counts in
   this document.

The backend suite never collects files under `tasks/`; `evals/conftest.py`
excludes them.

### How a pass is verified

pytest's exit code is not trusted on its own: candidate code that calls
`os._exit(0)` at import makes pytest exit 0 without running a test. Each run
writes a JUnit report into a fresh directory. A run counts as passed only if
pytest exits 0, the report exists, and exactly as many tests passed as
`pytest --collect-only` finds on the reference solution, with no failures,
errors or skips. The repository tests and the hidden tests run in two
separate fresh copies. The hidden tests do not exist on disk while the
candidate runs the first time, and nothing that run writes (edited tests, a
planted `conftest.py` or `sitecustomize.py`) carries over.

The target file is resolved from the issue text with
`agents.extract_target_file`, as `implementer_node` does when no plan names
it. If that gives no path or a different path, the task is recorded as
`target_mismatch`: unresolved, not an error, and no model call is made.

### Reading results

`results.json` has three parts:

- `metadata`: `mode`, `prompt_version` (hash of the prompt template),
  `git_sha` and `git_dirty`, `taskset_hash` (content hash of the evaluated
  tasks), `task_ids`, `timeout_s`, Python and platform, and start time. Live
  runs also record `model`, `temperature` and `seed`. Replay runs record the
  values stored in the cassettes and `synthetic_cassettes`.
- `aggregate`:
  - `n`, `resolved` and `resolve_rate`;
  - `resolve_rate_ci95`, a 95% Wilson score interval;
  - `output_rejected` and its rate;
  - `regressions` and its rate;
  - `target_mismatches`, `timed_out`, `errors` and `stale_cassettes`;
  - `latency_ms_mean` and `latency_ms_median`;
  - `tokens` (input, output and total), when the provider reports them.

  When `errors > 0`, `resolve_rate` and `resolve_rate_ci95` are `null`: the
  run is incomplete, and neither counting errored tasks as failures nor
  dropping them gives an honest rate.
- `tasks`: one record per task:

| Field | Meaning |
| --- | --- |
| `resolved` | The hidden tests pass and the pre-existing tests still pass (fail-to-pass plus pass-to-pass). |
| `hidden_tests_passed` | The hidden tests alone pass. |
| `regression` | A pre-existing test failed after the change. `null` if the change was never applied. |
| `target_mismatch`, `target_resolved` | `extract_target_file` on the issue text did not return the task's `target_file`; `target_resolved` is what it returned. |
| `output_rejected`, `rejection_reason` | `normalize_generated_code` raised: refusal, truncation (`finish_reason=length`), ambiguous fences or empty output. Production aborts the run in this case. |
| `patch` | Unified diff of the applied change, for failure analysis. |
| `test_output` | Tail of the failing pytest output. |
| `stale_cassette` | The cassette was recorded for a different prompt. Its result does not describe the current prompt. |
| `error` | An infrastructure failure, such as a missing cassette or a network error. The run exits with code 2. Do not report a run with errors. |

Report the interval with the rate, for example "18/30 resolved (60%, 95% CI
42–75%)". With n=30 the interval is roughly ±17 points around 50%.

### The committed cassettes

`cassettes/synthetic/` is hand-written. It is not output from a real model.
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

Each cassette's `note` says which case it is. Replaying the set measures the
harness and the output normalizer, not the model. CI gates on its known score
(18/30, `--min-resolve-rate 0.6`), and `evals/tests/test_harness.py` pins each
failure class per task and the 18/30 total. A change to
`normalize_generated_code` that starts rejecting well-formed output, or
accepting malformed output, therefore fails the PR. To track real model
behaviour offline, record a set with
`--mode live --record evals/cassettes/<name>` and replay it with
`--cassettes`.

Every cassette stores the hash of the prompt it answered. CI replays with
`--fail-on-stale`, so a PR that changes the Implementer prompt fails until the
cassettes are dealt with explicitly. For the synthetic set, run
`python -m evals.rehash`: its responses test the normalizer, not the model, so
re-binding them is correct. `evals.rehash` refuses real-model cassettes; those
must be re-recorded, because their answers were for the old prompt.

### Determinism

Live mode calls the model with `temperature=0` and a fixed `seed`; both are
saved in the results. Groq treats `seed` as best effort, so outputs can still
vary between calls, for example after backend or model updates. Use replay
when you need byte-for-byte reproducibility. Hidden tests run with
`PYTHONHASHSEED=0` and with pytest plugin autoloading disabled. Results are
sorted by task id, so `--jobs` does not change them.

### Comparing two versions fairly

The principles apply to both harnesses, but `python -m evals.compare`
reads only Implementer results (`evals.run`): it expects a top-level
`tasks` list, which end-to-end reports do not have (they use
`pipeline.runs` and `reviewer.runs`), and fails on one. For the end-to-end
harness, pair the runs yourself, as in
[Comparing Reviewer backends](#comparing-reviewer-backends-525).

1. Use the same tasks. `python -m evals.compare` refuses to compare when the
   `taskset_hash` values differ, and exits 2. In an end-to-end report,
   `taskset_hash` covers only the tasks used, not the Reviewer cases, so
   two Reviewer runs on different `--cases` get the same hash; check
   `--cases` yourself.
2. Change one thing at a time. The comparison lists any differences in mode,
   model, temperature, seed or timeout as `uncontrolled_settings`.
3. Compare paired outcomes, task by task, not two separate rates. Only the
   tasks where exactly one version succeeded carry information. `compare`
   lists them (`only_a_resolved` and `only_b_resolved`) and gives the exact
   McNemar p-value. Even 5 discordant tasks that all favour one side give
   only p=0.0625; it takes 6 of 6 (p=0.031) to reach p<0.05. Most
   differences at n=30 are noise.
4. Overlapping Wilson intervals are not a valid test of a difference,
   because they ignore the pairing. Use the McNemar result.
5. Run live comparisons more than once (epic #414 asks for at least 3
   seeds), and record real-model cassettes so a reviewer can replay exactly
   what you measured.
6. Read the failure `patch` and `test_output` fields, not only the totals. A
   higher score can come from tasks that are easier for one prompt by chance.

### Limitations

- **Small n, wide intervals.** 30 tasks give a 95% CI about 30 to 35 points
  wide in the middle of the range (18/30: 42–75%). Use the harness to catch
  large regressions and to inspect failures, not to rank close alternatives.
  Per-category counts (2 to 7 tasks) are too small for per-category rates.
- **Single-file, small Python tasks.** The Implementer rewrites one whole
  file. The tasks contain no multi-file edits, no other languages, no large
  files and no tasks where the model must find the right file.
- **Implementer only.** The plain completion here replaces production's
  GitNexus MCP tool loop (`run_llm_with_tools`), which also runs at the
  provider's default temperature.
- **Synthetic tasks.** They were written for this harness, which keeps them
  free of contamination, but they are easier and cleaner than real issues.
- **Not a sandbox.** Model-written code runs in a local `pytest` subprocess
  with a minimal environment (no API keys) and a timeout, but with network
  and file-system access, as the same user. A stripped environment does not
  hide secrets: on Linux the subprocess can read `/proc/<ppid>/environ` of
  the `evals.run` process, which holds `GROQ_API_KEY` in a one-step live run.
  Use `--generate-only` and score the recorded cassettes in a separate
  process without the key, as the workflow's `live` job does. Code that
  targets the harness itself (for example by forging the JUnit report it can
  locate) is out of scope for these checks. Run live evaluations only where
  that is acceptable. Running them inside a sandbox backend
  ([VERIFICATION.md](VERIFICATION.md#sandbox-backends)) is still open (#532).
- **One sample per task.** There are no repeated seeds or bootstrap
  intervals.
- **Not production quality.** Live mode differs from production in three
  ways at once: no MCP tool loop, `temperature=0` instead of the provider
  default, and one sample per task. Read a live resolve rate as a regression
  signal for the prompt and normalizer, not as the Implementer's quality in
  production.
