"""The whole AutoMaintainerEngine run offline on one task, per agent mode.

Every run here is the real engine (real graph nodes, verification and merge
policy) against the git-backed fake GitHub and the in-memory fake Supabase;
only the model is scripted. Nothing reaches GitHub, Supabase or Groq.
"""

import asyncio
import json
import sys

import pytest

from evals.e2e import cassettes
from evals.e2e.policies import (
    APPROVE,
    REJECT,
    ReplayTransport,
    ScriptedTransport,
    noop_pipeline,
    oracle_pipeline,
)
from evals.e2e.runner import run_pipeline_task
from evals.e2e.taskset import load_e2e_tasks

TASK = load_e2e_tasks(["inflection_ordinal_teens"])[0]


@pytest.fixture(autouse=True)
def _verify_with_this_python(monkeypatch):
    # verification.py starts the tests with VERIFY_PYTHON (a host setting,
    # not per engine); the backend environment has pytest.
    monkeypatch.setenv("VERIFY_PYTHON", sys.executable)


def run(transport, tmp_path, **kwargs):
    return asyncio.run(
        run_pipeline_task(TASK, transport=transport, root=tmp_path, **kwargs)
    )


def test_oracle_run_resolves_and_merges_without_help(tmp_path):
    transport = ScriptedTransport(oracle_pipeline(TASK))
    record = run(transport, tmp_path)
    assert record["error"] is None
    assert record["status"] == "completed"
    assert record["attempted"] and record["pr_opened"]
    assert record["maintainer_approved"] is True
    assert record["resolved"] is True and record["regression"] is False
    assert record["merged_without_help"] is True
    assert record["fix_iterations"] == 1
    # The Maintainer reviewed a head the engine itself verified.
    assert record["verification"] == "passed"
    assert [c["role"] for c in transport.calls] == ["pm", "implementer", "maintainer"]
    assert record["llm_calls"] == 3
    assert record["latency_s"] > 0


def test_noop_run_resolves_nothing(tmp_path):
    record = run(ScriptedTransport(noop_pipeline(TASK)), tmp_path)
    assert record["error"] is None
    assert record["attempted"] is True
    assert record["maintainer_approved"] is (record["status"] == "completed")
    assert record["resolved"] is False and record["merged_without_help"] is False


def test_rejection_triggers_another_iteration(tmp_path):
    reviews = iter([REJECT, APPROVE])
    oracle = oracle_pipeline(TASK)

    def policy(role, system, user):
        return next(reviews) if role == "maintainer" else oracle(role, system, user)

    record = run(ScriptedTransport(policy), tmp_path)
    assert record["fix_iterations"] == 2
    assert record["maintainer_approved"] and record["resolved"]
    assert record["merged_without_help"] is True


def test_approved_but_wrong_fix_is_not_merged_without_help(tmp_path):
    noop = noop_pipeline(TASK)
    original = noop("implementer", "", "")

    def policy(role, system, user):
        if role == "implementer":  # a change that does not fix the bug
            return original.replace("(11,)", "(11, 12)")
        return APPROVE

    record = run(ScriptedTransport(policy), tmp_path)
    assert record["pr_opened"] and record["maintainer_approved"]
    assert record["hidden_tests_passed"] is False
    assert record["merged_without_help"] is False


def test_pm_rejection_is_not_attempted(tmp_path):
    record = run(ScriptedTransport(lambda role, s, u: REJECT), tmp_path)
    assert record["status"] == "completed"
    assert record["attempted"] is False and record["pr_opened"] is False
    assert record["fix_iterations"] == 0 and record["resolved"] is False


def test_usage_gives_tokens_and_cost(tmp_path):
    oracle = oracle_pipeline(TASK)
    transport = ScriptedTransport(
        oracle, usage={"input_tokens": 1000, "output_tokens": 100}
    )
    record = run(transport, tmp_path, model="llama-3.3-70b-versatile")
    assert record["prompt_tokens"] == 3000 and record["completion_tokens"] == 300
    # 3000 * 0.59/1e6 + 300 * 0.79/1e6
    assert record["cost_usd"] == pytest.approx(0.002007)


def test_record_then_replay_reproduces_the_run(tmp_path):
    recorder = ScriptedTransport(oracle_pipeline(TASK))
    first = run(recorder, tmp_path / "a")
    cassette = cassettes.pipeline_cassette(TASK.id, [recorder.calls], source="oracle")
    path = cassettes.save(tmp_path / "set", cassette)
    replay = ReplayTransport(cassettes.load(path), repeat=0)
    second = run(replay, tmp_path / "b")
    assert second["error"] is None
    assert second["stale_calls"] == 0
    for key in ("status", "resolved", "merged_without_help", "fix_iterations"):
        assert second[key] == first[key]


def test_replay_marks_changed_prompts_stale(tmp_path):
    recorder = ScriptedTransport(oracle_pipeline(TASK))
    run(recorder, tmp_path / "a")
    for call in recorder.calls:
        call["prompt_sha256"] = "0" * 64
    cassette = cassettes.pipeline_cassette(TASK.id, [recorder.calls], source="oracle")
    record = run(ReplayTransport(cassette, repeat=0), tmp_path / "b")
    assert record["stale_calls"] == 3
    assert record["resolved"] is True


def test_replay_that_runs_out_of_answers_is_a_harness_error(tmp_path):
    cassette = cassettes.pipeline_cassette(
        TASK.id, [[{"role": "pm", "response": APPROVE, "prompt_sha256": ""}]], "oracle"
    )
    record = run(ReplayTransport(cassette, repeat=0), tmp_path)
    assert record["error"] and "cassette" in record["error"]
    assert record["resolved"] is False


def test_cassette_files_are_stable_json(tmp_path):
    cassette = cassettes.pipeline_cassette(
        TASK.id, [[{"role": "pm", "response": APPROVE, "prompt_sha256": "x"}]], "noop"
    )
    path = cassettes.save(tmp_path, cassette)
    assert json.loads(path.read_text(encoding="utf-8"))["source"] == "noop"
    assert cassettes.load(path) == cassette


def test_prompt_normalisation_ignores_volatile_text():
    a = f"Command: {sys.executable} -q\n2 passed in 0.31s\nhead 0123456789abcdef0123456789abcdef01234567"
    b = f"Command: {sys.executable} -q\n2 passed in 1.07s\nhead fedcba9876543210fedcba9876543210fedcba98"
    assert cassettes.prompt_hash("s", a) == cassettes.prompt_hash("s", b)
    assert cassettes.prompt_hash("s", a) != cassettes.prompt_hash("s", a + "x")


def test_prompt_normalisation_drops_the_verification_output_only():
    def prompt(status, output):
        return (
            f"Verification of this iteration: status={status}, exit code 0\n"
            f'<untrusted-data source="verification output">\n{output}\n</untrusted-data>'
        )

    linux = prompt("passed", "Command: python -q\n..  [100%]\n2 passed")
    windows = prompt(
        "passed", "Command: C:\\py.exe -q\n.. [100%]\r\n2 passed, 1 warning"
    )
    assert cassettes.prompt_hash("s", linux) == cassettes.prompt_hash("s", windows)
    failed = prompt("failed", "Command: python -q\n..  [100%]\n2 passed")
    assert cassettes.prompt_hash("s", failed) != cassettes.prompt_hash("s", linux)
