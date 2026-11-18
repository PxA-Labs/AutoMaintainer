"""``--reviewer-backend`` of ``python -m evals.e2e.run`` (#525).

The flag selects the engine's ``reviewer_backend`` so the e2e Reviewer
suite can compare the single-shot review with the ADK Reviewer. The default
stays ``langgraph``, so every replay of the committed cassettes is unchanged.
ADK makes its own model calls (not through the harness transport), so it is
accepted in live mode only: in oracle, noop or replay mode it would call a
real provider with the offline key, or replay nothing.
"""

import json
import sys

import pytest

from adk_fakes import APPROVE, ModelFactory, calls, needs_adk, scripted
from automaintainer import config as config_module
from automaintainer._runtime.specialists import reviewer_adk
from evals.e2e import run, runner
from evals.e2e.policies import ScriptedTransport, noop_reviewer
from evals.e2e.taskset import load_review_cases

GOOD_CASE = "good_inflection_ordinal_teens"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("VERIFY_PYTHON", sys.executable)
    for name in ["GROQ_API_KEY"] + [f"GROQ_API_KEY_{i}" for i in range(1, 20)]:
        monkeypatch.delenv(name, raising=False)


def test_default_is_langgraph_everywhere(tmp_path):
    args = run.parse_args(["--mode", "replay"])
    assert args.reviewer_backend == "langgraph"
    assert runner.make_config(tmp_path).reviewer_backend == "langgraph"


def test_make_config_passes_the_backend(tmp_path, monkeypatch):
    monkeypatch.setattr(config_module, "_adk_missing", lambda: None)
    config = runner.make_config(tmp_path, reviewer_backend="adk")
    assert config.reviewer_backend == "adk"


@pytest.mark.parametrize("mode", ["oracle", "noop", "replay"])
def test_adk_is_refused_outside_live_mode(mode, capsys):
    with pytest.raises(SystemExit) as raised:
        run.parse_args(["--mode", mode, "--reviewer-backend", "adk"])
    assert raised.value.code == 2
    assert "--reviewer-backend adk needs --mode live" in capsys.readouterr().err


def test_unknown_backend_is_refused():
    with pytest.raises(SystemExit):
        run.parse_args(["--mode", "live", "--reviewer-backend", "crew"])


def test_replay_reviewer_suite_is_unchanged_by_the_flag(tmp_path):
    """The committed fixture replay still scores exactly as documented, and
    the report records which backend reviewed."""
    out = tmp_path / "replay.json"
    code = run.main(
        ["--mode", "replay", "--suite", "reviewer", "--out", str(out)]
        + ["--fail-on-stale", "--jobs", "4"]
    )
    data = json.loads(out.read_text(encoding="utf-8"))
    assert code == 0
    assert data["metadata"]["engine"]["reviewer_backend"] == "langgraph"
    runs = data["reviewer"]["runs"]
    defective = [r for r in runs if r["expected_verdict"] == "reject"]
    approved = [r for r in defective if r["verdict"] == "approve"]
    assert (len(approved), len(defective)) == (3, 14)
    assert all(not r.get("stale_calls") for r in runs)


@needs_adk
def test_live_reviewer_case_with_the_adk_backend(tmp_path, monkeypatch):
    """End to end in the eval world with a fake ADK model: the real git
    checkout of the PR head (a file: URL), the real tools, the engine's own
    key, and a verdict scored like any other."""
    monkeypatch.setenv("GROQ_API_KEY", "eval-test-key")
    factory = ModelFactory(
        policy=scripted(
            calls(("read_file", {"path": "inflection/__init__.py"})), APPROVE
        )
    )
    monkeypatch.setattr(reviewer_adk, "build_model", factory)
    (case,) = load_review_cases([GOOD_CASE])
    out = tmp_path / "live.json"

    code = run.main(
        ["--mode", "live", "--suite", "reviewer", "--cases", GOOD_CASE]
        + ["--repeats", "1", "--reviewer-backend", "adk", "--out", str(out)],
        live_transport_factory=lambda a: ScriptedTransport(noop_reviewer(case)),
    )

    data = json.loads(out.read_text(encoding="utf-8"))
    assert code == 0, data["reviewer"]["runs"]
    assert data["metadata"]["engine"]["reviewer_backend"] == "adk"
    (record,) = data["reviewer"]["runs"]
    assert record["verdict"] == "approve" and record["correct"] is True
    assert record["llm_calls"] == 0  # the ADK model, not the transport
    (model,) = factory.built
    assert model.api_key == "eval-test-key"
    assert "def ordinal" in "".join(model.turns[-1].tool_results)
