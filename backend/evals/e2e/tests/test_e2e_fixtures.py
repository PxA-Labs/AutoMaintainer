"""The committed replay fixtures: complete, synthetic, mixed by the documented
rule, and replayable. CI replays the whole set with exact score gates."""

import asyncio
import sys

import pytest

from evals.e2e import cassettes
from evals.e2e.make_fixtures import expected_scores
from evals.e2e.policies import ReplayTransport
from evals.e2e.runner import run_pipeline_task, run_review_case
from evals.e2e.taskset import load_e2e_tasks, load_review_cases

TASKS = load_e2e_tasks()
CASES = load_review_cases()
SCORES = expected_scores([t.id for t in TASKS], CASES)


@pytest.fixture(autouse=True)
def _verify_with_this_python(monkeypatch):
    monkeypatch.setenv("VERIFY_PYTHON", sys.executable)


def test_every_task_and_case_has_a_synthetic_fixture():
    for kind, ids, noop in (
        ("pipeline", [t.id for t in TASKS], SCORES["noop_tasks"]),
        ("reviewer", [c.id for c in CASES], SCORES["noop_cases"]),
    ):
        stored = sorted(p.stem for p in (cassettes.FIXTURES_DIR / kind).glob("*.json"))
        assert stored == sorted(ids)
        for cid in ids:
            cassette = cassettes.load_for(cassettes.FIXTURES_DIR, kind, cid)
            assert cassette["synthetic"] is True
            assert cassette["source"] == ("noop" if cid in noop else "oracle")
            assert "no model output" in cassette["note"]


def test_expected_scores_are_the_documented_ones():
    assert SCORES["resolved_rate"] == pytest.approx(16 / 24)
    assert SCORES["false_approval_rate"] == pytest.approx(3 / 14)


@pytest.mark.parametrize(
    "task_id", [SCORES["noop_tasks"][0], "inflection_ordinal_teens"]
)
def test_pipeline_fixture_replays_without_stale_calls(task_id, tmp_path):
    item = next(t for t in TASKS if t.id == task_id)
    cassette = cassettes.load_for(cassettes.FIXTURES_DIR, "pipeline", task_id)
    record = asyncio.run(
        run_pipeline_task(item, transport=ReplayTransport(cassette), root=tmp_path)
    )
    assert record["error"] is None and record["stale_calls"] == 0
    assert record["resolved"] is (task_id not in SCORES["noop_tasks"])


def test_reviewer_fixture_replays_a_false_approval(tmp_path):
    case = next(c for c in CASES if c.id == SCORES["noop_cases"][0])
    item = next(t for t in TASKS if t.id == case.task_id)
    cassette = cassettes.load_for(cassettes.FIXTURES_DIR, "reviewer", case.id)
    record = asyncio.run(
        run_review_case(item, case, transport=ReplayTransport(cassette), root=tmp_path)
    )
    assert record["error"] is None and record["stale_calls"] == 0
    assert record["verdict"] == "approve" and record["correct"] is False
