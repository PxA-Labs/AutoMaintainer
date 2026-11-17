"""``python -m evals.e2e.run``: modes, gates, exit codes, report files."""

import json
import sys

import pytest

from evals.e2e import run
from evals.e2e.policies import ScriptedTransport, oracle_pipeline

TASKS = "inflection_ordinal_teens,boltons_ceil_unsorted_options"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("VERIFY_PYTHON", sys.executable)
    for name in ["GROQ_API_KEY"] + [f"GROQ_API_KEY_{i}" for i in range(1, 20)]:
        monkeypatch.delenv(name, raising=False)


def _run(tmp_path, name, *args, **kwargs):
    out = tmp_path / f"{name}.json"
    code = run.main([*args, "--out", str(out), "--jobs", "2"], **kwargs)
    data = json.loads(out.read_text(encoding="utf-8")) if out.exists() else None
    return code, data, out


def test_oracle_pipeline_passes_its_gate_and_writes_both_reports(tmp_path):
    code, data, out = _run(
        tmp_path, "oracle", "--mode", "oracle", "--suite", "pipeline",
        "--tasks", TASKS, "--min-resolve-rate", "1.0",
    )  # fmt: skip
    assert code == 0
    meta = data["metadata"]
    assert meta["mode"] == "oracle" and meta["repeats"] == 1
    assert meta["task_ids"] == sorted(TASKS.split(","))
    for key in (
        "git_sha",
        "taskset_hash",
        "python",
        "started_at",
        "price_source",
        "engine",
    ):
        assert key in meta
    assert meta["engine"]["merge_policy"] == "human"
    overall = data["pipeline"]["summary"]["overall"]
    assert overall["resolved_rate"]["mean"] == 1.0
    assert overall["merged_without_help_rate"]["mean"] == 1.0
    assert data["reviewer"] is None
    markdown = out.with_suffix(".md").read_text(encoding="utf-8")
    assert "Merged without help" in markdown


def test_noop_fails_a_resolve_gate_with_exit_code_1(tmp_path):
    code, data, _ = _run(
        tmp_path, "noop", "--mode", "noop", "--suite", "pipeline",
        "--tasks", "inflection_ordinal_teens", "--min-resolve-rate", "0.5",
    )  # fmt: skip
    assert code == 1
    assert data["pipeline"]["summary"]["overall"]["resolved_rate"]["mean"] == 0.0


def test_reviewer_gates(tmp_path):
    cases = "bad_ordinal_partial_teens,good_inflection_ordinal_teens"
    code, data, _ = _run(
        tmp_path, "rev-oracle", "--mode", "oracle", "--suite", "reviewer",
        "--cases", cases, "--max-false-approval-rate", "0", "--max-false-rejection-rate", "0",
    )  # fmt: skip
    assert code == 0
    assert data["reviewer"]["summary"]["overall"]["false_approval_rate"]["mean"] == 0.0
    code, data, _ = _run(
        tmp_path, "rev-noop", "--mode", "noop", "--suite", "reviewer",
        "--cases", cases, "--max-false-approval-rate", "0",
    )  # fmt: skip
    assert code == 1
    assert data["reviewer"]["summary"]["overall"]["false_approval_rate"]["mean"] == 1.0


def test_record_then_replay_without_stale_calls(tmp_path):
    cassettes = tmp_path / "cassettes"
    code, recorded, _ = _run(
        tmp_path, "rec", "--mode", "oracle", "--tasks", "inflection_ordinal_teens",
        "--cases", "bad_ordinal_partial_teens", "--record", str(cassettes),
    )  # fmt: skip
    assert code == 0
    assert (cassettes / "pipeline" / "inflection_ordinal_teens.json").is_file()
    assert (cassettes / "reviewer" / "bad_ordinal_partial_teens.json").is_file()
    code, replayed, _ = _run(
        tmp_path, "replay", "--mode", "replay", "--cassettes", str(cassettes),
        "--tasks", "inflection_ordinal_teens", "--cases", "bad_ordinal_partial_teens",
        "--fail-on-stale", "--min-resolve-rate", "1.0", "--max-false-approval-rate", "0",
    )  # fmt: skip
    assert code == 0
    assert replayed["metadata"]["synthetic"] is True
    assert replayed["pipeline"]["summary"]["overall"]["stale_calls"] == 0


def test_replay_without_a_cassette_is_a_harness_error(tmp_path):
    code, data, _ = _run(
        tmp_path, "missing", "--mode", "replay", "--cassettes", str(tmp_path / "none"),
        "--suite", "pipeline", "--tasks", "inflection_ordinal_teens",
    )  # fmt: skip
    assert code == 2
    assert data["pipeline"]["summary"]["overall"]["errors"] == 1


def test_live_needs_a_key(tmp_path):
    code, data, _ = _run(
        tmp_path, "live", "--mode", "live", "--tasks", "inflection_ordinal_teens"
    )
    assert code == 2 and data is None


def test_repeats_default_to_three_only_in_live():
    assert run.parse_args(["--mode", "live"]).repeats == 3
    assert run.parse_args(["--mode", "oracle"]).repeats == 1


def test_live_mode_uses_the_key_pool_and_records_usage(tmp_path, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "key-a")
    monkeypatch.setenv("GROQ_API_KEY_1", "key-b")
    item = run.load_e2e_tasks(["inflection_ordinal_teens"])[0]
    seen = []

    class Recording(ScriptedTransport):
        async def __call__(self, *, system_prompt, user_prompt, model, api_key):
            seen.append((model, api_key))
            return await super().__call__(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                model=model,
                api_key=api_key,
            )

    def factory(args):
        return Recording(
            oracle_pipeline(item), usage={"input_tokens": 10, "output_tokens": 2}
        )

    code, data, _ = _run(
        tmp_path, "live", "--mode", "live", "--suite", "pipeline", "--repeats", "1",
        "--tasks", "inflection_ordinal_teens", live_transport_factory=factory,
    )  # fmt: skip
    assert code == 0
    assert {key for _, key in seen} <= {"key-a", "key-b"}
    assert {model for model, _ in seen} == {"llama-3.3-70b-versatile"}
    overall = data["pipeline"]["summary"]["overall"]
    assert overall["prompt_tokens"]["mean"] == 30
    assert overall["cost_usd"]["mean"] is not None
    assert data["metadata"]["synthetic"] is False
