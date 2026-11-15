import json

import pytest

from evals import compare, run
from evals.llm import Completion, load_cassette
from evals.prompt import PROMPT_VERSION


def _run(tmp_path, name, *args, completer=None):
    out = tmp_path / f"{name}.json"
    code = run.main([*args, "--out", str(out), "--jobs", "2"], completer=completer)
    data = json.loads(out.read_text(encoding="utf-8")) if out.exists() else None
    return code, data


def test_oracle_gate_passes_and_metadata_is_recorded(tmp_path):
    code, data = _run(
        tmp_path,
        "oracle",
        "--mode",
        "oracle",
        "--tasks",
        "page_count,slug_hyphens",
        "--min-resolve-rate",
        "1.0",
    )
    assert code == 0
    meta = data["metadata"]
    assert meta["mode"] == "oracle" and meta["baseline"] == "oracle"
    assert meta["prompt_version"] == PROMPT_VERSION
    assert meta["task_ids"] == ["page_count", "slug_hyphens"]
    for key in ("git_sha", "git_dirty", "taskset_hash", "python", "started_at"):
        assert key in meta
    assert data["aggregate"]["resolve_rate"] == 1.0


def test_resolve_rate_gates_fail_with_exit_code_1(tmp_path):
    code, _ = _run(
        tmp_path, "noop", "--mode", "noop", "--tasks", "page_count",
        "--min-resolve-rate", "0.5",
    )  # fmt: skip
    assert code == 1
    code, _ = _run(
        tmp_path, "oracle", "--mode", "oracle", "--tasks", "page_count",
        "--max-resolve-rate", "0.0",
    )  # fmt: skip
    assert code == 1


def test_live_mode_without_api_key_fails_fast(tmp_path, monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    code, data = _run(tmp_path, "live", "--mode", "live", "--tasks", "page_count")
    assert code == 2 and data is None


def test_replay_never_builds_the_live_client(tmp_path, monkeypatch):
    def forbidden(*_a, **_k):
        raise AssertionError("replay must not create an LLM client")

    monkeypatch.setattr(run, "groq_completer", forbidden)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    code, data = _run(tmp_path, "replay", "--mode", "replay", "--tasks", "page_count")
    assert code == 0
    assert data["metadata"]["synthetic_cassettes"] is True
    assert data["metadata"]["model"] == ["synthetic-fixture"]


def test_live_records_cassettes_that_replay_identically(tmp_path):
    def fake_model(system_prompt, user_prompt):
        return Completion(
            text="def page_count(total_items, per_page):\n    return 0\n",
            finish_reason="stop",
            usage={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
            latency_ms=7,
        )

    cassettes = tmp_path / "cassettes"
    code, live = _run(
        tmp_path, "live", "--mode", "live", "--tasks", "page_count",
        "--record", str(cassettes), "--seed", "7", "--model", "test-model",
        completer=fake_model,
    )  # fmt: skip
    assert code == 0
    assert live["metadata"]["temperature"] == 0.0
    assert (live["metadata"]["seed"], live["metadata"]["model"]) == (7, "test-model")
    record = load_cassette(cassettes, "page_count")
    assert record["synthetic"] is False and record["seed"] == 7

    code, replay = _run(
        tmp_path, "replay", "--mode", "replay", "--tasks", "page_count",
        "--cassettes", str(cassettes),
    )  # fmt: skip
    assert code == 0
    keys = ("resolved", "regression", "output_rejected", "patch", "usage")
    assert [{k: r[k] for k in keys} for r in replay["tasks"]] == [
        {k: r[k] for k in keys} for r in live["tasks"]
    ]
    assert replay["tasks"][0]["stale_cassette"] is False
    # The broken rewrite drops page_bounds: a regression, not a resolve.
    assert replay["tasks"][0]["regression"] is True


def test_paired_comparison(tmp_path):
    _, oracle = _run(tmp_path, "o", "--mode", "oracle", "--tasks", "page_count")
    _, noop = _run(tmp_path, "n", "--mode", "noop", "--tasks", "page_count")
    report = compare.compare(noop, oracle)
    assert report["only_b_resolved"] == ["page_count"]
    assert report["only_a_resolved"] == []
    assert report["difference"] == 1.0
    assert report["mcnemar_exact_p"] == 1.0  # one discordant task proves nothing
    assert report["uncontrolled_settings"] == {"mode": ["noop", "oracle"]}


def test_comparison_refuses_different_task_sets(tmp_path):
    _, a = _run(tmp_path, "a", "--mode", "noop", "--tasks", "page_count")
    _, b = _run(tmp_path, "b", "--mode", "noop", "--tasks", "slug_hyphens")
    with pytest.raises(compare.ComparisonError, match="different task sets"):
        compare.compare(a, b)
    out_a, out_b = tmp_path / "a.json", tmp_path / "b.json"
    assert compare.main([str(out_a), str(out_b)]) == 2
