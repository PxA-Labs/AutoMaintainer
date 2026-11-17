"""Metrics over repeated e2e runs and the JSON/Markdown report."""

import pytest

from evals.e2e.metrics import mean_std, summarize_pipeline, summarize_reviewer
from evals.e2e.pricing import cost_usd, load_prices
from evals.e2e.report import build_report, render_markdown


def rec(task, repeat, resolved, approved=True, iterations=1, error=None, **extra):
    return {
        "kind": "pipeline",
        "id": task,
        "repeat": repeat,
        "attempted": iterations > 0,
        "resolved": resolved,
        "maintainer_approved": approved,
        "merged_without_help": approved and resolved,
        "fix_iterations": iterations,
        "prompt_tokens": extra.get("prompt_tokens"),
        "completion_tokens": extra.get("completion_tokens"),
        "cost_usd": extra.get("cost_usd"),
        "latency_s": extra.get("latency_s", 1.0),
        "stale_calls": 0,
        "error": error,
    }


def test_mean_std():
    assert mean_std([1, 2, 3]) == {"mean": 2.0, "std": 1.0, "n": 3}
    assert mean_std([4]) == {"mean": 4.0, "std": None, "n": 1}
    assert mean_std([None, None]) == {"mean": None, "std": None, "n": 0}


def test_pipeline_summary_per_task_and_over_repeats():
    records = [
        rec("a", 0, True, iterations=1, latency_s=2.0),
        rec("a", 1, False, iterations=3, latency_s=4.0),
        rec("b", 0, True, approved=False, iterations=2),
        rec("b", 1, True, iterations=1),
    ]
    summary = summarize_pipeline(records)
    a = summary["per_task"]["a"]
    assert a["runs"] == 2
    assert a["resolved_rate"] == 0.5
    assert a["fix_iterations"] == {
        "mean": 2.0,
        "std": pytest.approx(1.4142, abs=1e-4),
        "n": 2,
    }
    assert a["latency_s"]["mean"] == 3.0
    overall = summary["overall"]
    assert overall["repeats"] == 2 and overall["tasks"] == 2
    # repeat 0: a and b resolved; repeat 1: only b.
    assert overall["resolved_rate"] == {
        "mean": 0.75,
        "std": pytest.approx(0.3536, abs=1e-4),
        "n": 2,
    }
    # repeat 0: only a merged without help (b was rejected); repeat 1: b.
    assert overall["merged_without_help_rate"]["mean"] == 0.5
    assert overall["errors"] == 0
    assert overall["resolved_runs"] == 3 and overall["runs"] == 4


def test_errors_void_the_rates():
    summary = summarize_pipeline([rec("a", 0, True), rec("b", 0, False, error="boom")])
    assert summary["overall"]["errors"] == 1
    assert summary["overall"]["resolved_rate"] is None


def test_tokens_and_cost_are_summed_per_repeat():
    records = [
        rec("a", 0, True, prompt_tokens=100, completion_tokens=10, cost_usd=0.5),
        rec("b", 0, True, prompt_tokens=300, completion_tokens=30, cost_usd=1.5),
    ]
    overall = summarize_pipeline(records)["overall"]
    assert overall["prompt_tokens"]["mean"] == 400
    assert overall["completion_tokens"]["mean"] == 40
    assert overall["cost_usd"]["mean"] == 2.0
    unknown = summarize_pipeline([rec("a", 0, True)])["overall"]
    assert unknown["cost_usd"] == {"mean": None, "std": None, "n": 0}


def review(case, repeat, expected, verdict, defect_class=None, error=None):
    return {
        "kind": "reviewer",
        "id": case,
        "repeat": repeat,
        "expected_verdict": expected,
        "verdict": verdict,
        "defect_class": defect_class,
        "correct": None if verdict is None else verdict == expected,
        "error": error,
        "latency_s": 1.0,
        "prompt_tokens": None,
        "completion_tokens": None,
        "cost_usd": None,
    }


def test_reviewer_false_approval_and_false_rejection_rates():
    records = [
        review("bad1", 0, "reject", "approve", "wrong-logic"),
        review("bad2", 0, "reject", "reject", "deleted-test"),
        review("good1", 0, "approve", "reject"),
        review("good2", 0, "approve", "approve"),
        review("bad1", 1, "reject", "reject", "wrong-logic"),
        review("bad2", 1, "reject", "reject", "deleted-test"),
        review("good1", 1, "approve", "approve"),
        review("good2", 1, "approve", "approve"),
    ]
    summary = summarize_reviewer(records)
    overall = summary["overall"]
    assert overall["bad_changes"] == 2 and overall["correct_changes"] == 2
    assert overall["false_approval_rate"]["mean"] == 0.25
    assert overall["false_rejection_rate"]["mean"] == 0.25
    assert summary["by_defect_class"]["wrong-logic"] == {"runs": 2, "approved": 1}
    assert summary["per_case"]["bad1"]["approvals"] == 1


def test_reviewer_errors_void_the_rates():
    summary = summarize_reviewer(
        [review("bad1", 0, "reject", None, "wrong-logic", error="x")]
    )
    assert summary["overall"]["false_approval_rate"] is None


def test_cost_table(tmp_path):
    assert cost_usd("llama-3.3-70b-versatile", 1_000_000, 0) == pytest.approx(0.59)
    assert cost_usd("unknown-model", 1, 1) is None
    assert cost_usd("llama-3.3-70b-versatile", None, 1) is None
    path = tmp_path / "prices.json"
    path.write_text('{"m": {"input_per_mtok": 1, "output_per_mtok": 2}}')
    table, source = load_prices(path)
    assert cost_usd("m", 1_000_000, 1_000_000, table) == 3
    assert "prices.json" in source
    path.write_text('{"m": {"input_per_mtok": -1, "output_per_mtok": 2}}')
    with pytest.raises(ValueError):
        load_prices(path)


def test_report_json_and_markdown():
    pipeline = [rec("a", 0, True, prompt_tokens=10, completion_tokens=1, cost_usd=0.1)]
    reviewer = [review("bad1", 0, "reject", "reject", "wrong-logic")]
    report = build_report({"mode": "oracle", "repeats": 1}, pipeline, reviewer)
    assert set(report) == {"metadata", "pipeline", "reviewer"}
    assert report["pipeline"]["runs"] == pipeline
    text = render_markdown(report)
    assert "# AutoMaintainer end-to-end evaluation" in text
    assert "| a |" in text
    assert "False-approval rate" in text
    assert "oracle" in text
