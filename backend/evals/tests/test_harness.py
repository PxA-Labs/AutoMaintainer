"""Validate the harness itself: baselines, replay determinism, isolation."""

import json
import shutil

import pytest

from evals.harness import (
    aggregate,
    evaluate_task,
    expected_test_counts,
    run_pytest,
    run_tasks,
)
from evals.llm import CASSETTES_DIR, Completion, load_cassette
from evals.prompt import build_implementer_prompt
from evals.taskset import TASKS_DIR, load_task, load_tasks

TASKS = load_tasks()
# Outcome fields; timings and pytest output (temp paths, durations) vary.
OUTCOME_KEYS = (
    "task_id",
    "resolved",
    "hidden_tests_passed",
    "regression",
    "output_rejected",
    "rejection_reason",
    "applied",
    "finish_reason",
    "stale_cassette",
    "error",
    "patch",
)


def _outcomes(results):
    return [{k: r[k] for k in OUTCOME_KEYS} for r in results]


@pytest.fixture(scope="module")
def oracle_results():
    return run_tasks(TASKS, "oracle", jobs=4)


@pytest.fixture(scope="module")
def noop_results():
    return run_tasks(TASKS, "noop", jobs=4)


@pytest.fixture(scope="module")
def replay_results():
    return run_tasks(TASKS, "replay", jobs=4, cassettes_dir=CASSETTES_DIR)


def test_oracle_resolves_every_task(oracle_results):
    summary = aggregate(oracle_results)
    assert summary["resolve_rate"] == 1.0, [
        (r["task_id"], r["test_output"]) for r in oracle_results if not r["resolved"]
    ]
    assert summary["regressions"] == summary["output_rejected"] == 0
    assert all(r["applied"] for r in oracle_results)


def test_noop_resolves_nothing_and_breaks_nothing(noop_results):
    # Every hidden test file must fail on the unchanged repo (otherwise the
    # task is already solved or the tests are vacuous), while the repo's own
    # tests pass (otherwise "regression" would be meaningless).
    summary = aggregate(noop_results)
    assert summary["resolve_rate"] == 0.0
    for r in noop_results:
        assert r["hidden_tests_passed"] is False, r["task_id"]
        assert r["regression"] is False, (r["task_id"], r["test_output"])
        assert not r["applied"] and r["patch"] == ""


def test_replay_exercises_every_outcome(replay_results):
    by_id = {r["task_id"]: r for r in replay_results}
    assert by_id["lru_eviction"]["output_rejected"]
    assert "refused" in by_id["lru_eviction"]["rejection_reason"]
    assert by_id["interval_merge"]["output_rejected"]
    assert "truncated" in by_id["interval_merge"]["rejection_reason"]
    assert by_id["backoff_cap"]["regression"] is True
    assert not by_id["backoff_cap"]["resolved"]
    assert by_id["duration_seconds"]["hidden_tests_passed"] is False
    assert by_id["duration_seconds"]["regression"] is False
    # Chat-style fenced replies are unwrapped by normalize_generated_code.
    assert by_id["slug_hyphens"]["resolved"]
    assert by_id["env_bool_override"]["resolved"]
    assert by_id["percentile_nearest_rank"]["resolved"]  # one-line lead-in
    assert by_id["unique_keep_first"]["resolved"]  # chat explanation, py fence
    assert by_id["config_required_keys"]["resolved"]  # untagged fence only
    # Rejections the production Implementer must also refuse to commit.
    assert "empty" in by_id["batched_tail"]["rejection_reason"]
    assert "ambiguous" in by_id["transpose_ragged"]["rejection_reason"]
    assert "language" in by_id["report_clock_injection"]["rejection_reason"]
    # A bare file with a syntax error is applied, then breaks every test.
    assert by_id["carrier_registry"]["applied"]
    assert by_id["carrier_registry"]["regression"] is True
    # Dropping a public name existing callers use is a regression.
    assert by_id["deprecated_alias"]["regression"] is True
    # Plausible but wrong fixes: hidden tests fail, nothing else breaks.
    for task_id in (
        "sliding_window_limiter",
        "event_bus_unsubscribe",
        "port_validation",
    ):
        assert by_id[task_id]["hidden_tests_passed"] is False, task_id
        assert by_id[task_id]["regression"] is False, task_id
    summary = aggregate(replay_results)
    # CI's replay gate (--min-resolve-rate 0.6 in evals.yml) relies on this.
    assert (summary["resolved"], summary["n"]) == (18, 30)
    assert summary["output_rejected"] == 5 and summary["regressions"] == 3
    assert summary["stale_cassettes"] == 0
    assert all(r["synthetic"] for r in replay_results)


def test_replay_is_deterministic(replay_results):
    again = run_tasks(TASKS, "replay", jobs=2, cassettes_dir=CASSETTES_DIR)
    assert _outcomes(again) == _outcomes(replay_results)
    assert aggregate(again) == aggregate(replay_results)


def test_replay_flags_stale_cassettes(tmp_path):
    task = load_task(TASKS_DIR / "page_count")
    record = load_cassette(CASSETTES_DIR, task.id)
    record["prompt_sha256"] = "0" * 64  # recorded against another prompt
    (tmp_path / "page_count.json").write_text(json.dumps(record), encoding="utf-8")
    result = evaluate_task(task, "replay", cassettes_dir=tmp_path)
    assert result["stale_cassette"] is True
    assert aggregate([result])["stale_cassettes"] == 1


def test_missing_cassette_is_a_harness_error_not_a_failure(tmp_path):
    task = load_task(TASKS_DIR / "page_count")
    result = evaluate_task(task, "replay", cassettes_dir=tmp_path)
    assert result["error"].startswith("FileNotFoundError")
    assert aggregate([result])["errors"] == 1
    assert aggregate([result])["resolve_rate"] is None


def test_live_mode_sends_the_implementer_prompt_and_records_usage():
    task = load_task(TASKS_DIR / "page_count")
    seen = []
    solution = (task.solution_dir / task.target_file).read_text(encoding="utf-8")

    def fake_model(system_prompt, user_prompt):
        seen.append((system_prompt, user_prompt))
        return Completion(
            text=f"```python\n{solution}```",
            finish_reason="stop",
            usage={"input_tokens": 300, "output_tokens": 120, "total_tokens": 420},
            latency_ms=250,
        )

    result = evaluate_task(task, "live", completer=fake_model)
    assert seen == [
        build_implementer_prompt(task.issue_text, task.target_file, task.read_target())
    ]
    assert result["resolved"]
    summary = aggregate([result])
    assert summary["tokens"] == {
        "input_tokens": 300,
        "output_tokens": 120,
        "total_tokens": 420,
    }
    assert summary["latency_ms_mean"] == 250


def test_llm_failure_is_reported_as_error():
    task = load_task(TASKS_DIR / "page_count")

    def broken(*_):
        raise ConnectionError("network down")

    result = evaluate_task(task, "live", completer=broken)
    assert result["error"] == "ConnectionError: network down"
    assert not result["resolved"]


def _scratch_task(tmp_path, hidden_test: str):
    dest = tmp_path / "page_count"
    shutil.copytree(TASKS_DIR / "page_count", dest)
    for f in (dest / "hidden_tests").glob("*.py"):
        f.unlink()
    (dest / "hidden_tests" / "test_probe.py").write_text(hidden_test, "utf-8")
    return load_task(dest)


def test_hidden_tests_time_out(tmp_path):
    task = _scratch_task(
        tmp_path, "import time\n\n\ndef test_hangs():\n    time.sleep(30)\n"
    )
    result = evaluate_task(task, "oracle", timeout=3)
    assert result["timed_out"] is True
    assert not result["resolved"]
    assert "TIMEOUT" in result["test_output"]


def test_secrets_are_not_passed_to_model_written_code(tmp_path, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "sk-test-not-real")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp-test-not-real")
    task = _scratch_task(
        tmp_path,
        "import os\n\n\ndef test_env():\n"
        "    assert 'GROQ_API_KEY' not in os.environ\n"
        "    assert 'GITHUB_TOKEN' not in os.environ\n"
        "    assert os.environ['PYTHONHASHSEED'] == '0'\n",
    )
    result = evaluate_task(task, "oracle")
    assert result["resolved"], result["test_output"]


def test_aggregate_of_nothing():
    summary = aggregate([])
    assert summary["n"] == 0 and summary["resolve_rate"] is None
    assert summary["resolve_rate_ci95"] == [0.0, 1.0]


# --- reward hacking: a pass must be a verified pass --------------------------

_LEDGER = load_task(TASKS_DIR / "ledger_first_row")


def _live(task, text):
    return evaluate_task(
        task, "live", completer=lambda *_: Completion(text=text, finish_reason="stop")
    )


def test_interpreter_exit_zero_is_not_resolved():
    # Reviewer repro: os._exit(0) at import makes pytest exit 0 without
    # running a test. Before the JUnit check this scored resolved=True.
    original = _LEDGER.read_target()
    result = _live(_LEDGER, "import os\nos._exit(0)\n" + original)
    assert result["applied"] and not result["output_rejected"]
    assert result["hidden_tests_passed"] is False
    assert result["regression"] is True
    assert not result["resolved"]
    assert "no JUnit report" in result["test_output"]


def test_module_level_skip_is_not_resolved():
    original = _LEDGER.read_target()
    text = "import pytest\npytest.skip(allow_module_level=True)\n" + original
    result = _live(_LEDGER, text)
    assert not result["resolved"]
    assert result["hidden_tests_passed"] is False


def test_candidate_cannot_rewrite_hidden_tests_from_the_repo_test_run():
    # On import (first during the repo-test run) the candidate replaces every
    # hidden test with three trivially passing ones. The hidden tests used to
    # sit next to the repo copy at that point, so this scored as resolved.
    fake = "".join(f"def test_{c}():\n    pass\n\n\n" for c in "abc")
    tamper = (
        "from pathlib import Path as _P\n"
        "_h = _P(__file__).resolve().parent.parent / 'hidden_tests'\n"
        "for _f in (_h.glob('test_*.py') if _h.is_dir() else []):\n"
        f"    _f.write_text({fake!r})\n"
    )
    result = _live(_LEDGER, _LEDGER.read_target() + tamper)
    assert result["regression"] is False
    assert result["hidden_tests_passed"] is False
    assert not result["resolved"]


def test_expected_counts_come_from_the_reference_solution():
    repo_tests, hidden_tests = expected_test_counts(_LEDGER)
    assert hidden_tests == 3  # test_hidden_ledger.py defines three tests
    assert repo_tests > 0


def test_fewer_passing_tests_than_expected_is_a_failure(tmp_path):
    task = _scratch_task(tmp_path, "def test_one():\n    pass\n")
    work = tmp_path / "w"
    shutil.copytree(task.hidden_tests_dir, work)
    ini = tmp_path / "pytest.ini"
    ini.write_text("[pytest]\n", encoding="utf-8")
    assert run_pytest(work, work, ini, 60, expected=1).passed
    short = run_pytest(work, work, ini, 60, expected=2)
    assert not short.passed and "1 of 2 expected" in short.output


# --- target resolution and incomplete runs -----------------------------------


def test_unresolvable_target_is_a_task_outcome_not_an_error(tmp_path):
    dest = tmp_path / "page_count"
    shutil.copytree(TASKS_DIR / "page_count", dest)
    meta = json.loads((dest / "task.json").read_text(encoding="utf-8"))
    meta["title"] = "Paging rounds the wrong way"
    meta["body"] = "The page count drops the last partial page."
    (dest / "task.json").write_text(json.dumps(meta), encoding="utf-8")
    task = load_task(dest)

    def never_called(*_):
        raise AssertionError("the pipeline would stop before the model call")

    result = evaluate_task(task, "live", completer=never_called)
    assert result["target_mismatch"] is True
    assert result["target_resolved"] is None
    assert not result["resolved"] and result["error"] is None
    summary = aggregate([result])
    assert summary["target_mismatches"] == 1 and summary["resolve_rate"] == 0.0


def test_errors_make_the_rate_unavailable(tmp_path):
    task = load_task(TASKS_DIR / "page_count")
    ok = evaluate_task(task, "oracle")
    missing = evaluate_task(task, "replay", cassettes_dir=tmp_path)
    summary = aggregate([ok, missing])
    assert summary["errors"] == 1 and summary["resolved"] == 1
    assert summary["resolve_rate"] is None
    assert summary["resolve_rate_ci95"] is None


def test_generate_only_never_executes_the_candidate(tmp_path):
    sentinel = tmp_path / "ran.txt"
    text = f"open({str(sentinel)!r}, 'w').close()\n" + _LEDGER.read_target()
    seen = []
    result = evaluate_task(
        _LEDGER,
        "live",
        completer=lambda *_: Completion(text=text, finish_reason="stop"),
        on_completion=lambda *a: seen.append(a[0].id),
        score=False,
    )
    assert seen == ["ledger_first_row"]
    assert not sentinel.exists()
    assert result["hidden_tests_passed"] is None and not result["applied"]
