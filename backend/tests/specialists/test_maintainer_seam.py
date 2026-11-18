"""The one seam in ``maintainer_node``: the review goes to the selected
backend, and everything after it (comment, merge policy, trust gate,
checkpoint) is the node's own, whichever backend reviewed (#525)."""

from contextlib import asynccontextmanager
from pathlib import Path

import pytest

import agents
from adk_fakes import APPROVE, REJECT, ModelFactory, calls, needs_adk, scripted
from automaintainer import config as config_module
from automaintainer.config import AutoMaintainerConfig
from automaintainer.context import EngineContext, current_engine
from automaintainer._runtime.specialists import reviewer_adk
from test_agent_merge_policy import _setup, _state


@pytest.fixture(autouse=True)
def _no_merge_policy_env(monkeypatch):
    monkeypatch.delenv("MERGE_POLICY", raising=False)
    monkeypatch.delenv("REVIEWER_BACKEND", raising=False)


@pytest.fixture
def recorded(monkeypatch):
    """Replace the ADK entry point; record what the node hands it."""
    seen = []

    async def fake(state, gh_repo, pr, diff_text):
        seen.append({"state": state, "pr": pr, "diff": diff_text})
        return agents.MaintainerDecision(verdict="approve", reasons=["adk"]), None

    monkeypatch.setattr(reviewer_adk, "review_pull_request", fake)
    return seen


def engine_context(backend, monkeypatch):
    monkeypatch.setattr(config_module, "_adk_missing", lambda: None)
    config = AutoMaintainerConfig(groq_api_keys=["k"], reviewer_backend=backend)
    return EngineContext(config=config, engine_id="e-seam", rate_limiter=lambda: None)


async def test_default_backend_never_reaches_the_adk_reviewer(monkeypatch, recorded):
    _, pr, seen = _setup(monkeypatch)

    result = await agents.maintainer_node(_state())

    assert recorded == []
    assert seen["system"].startswith("You are the Maintainer")
    assert result["merge_status"] == "awaiting_human_review"


async def test_env_selects_adk_without_an_engine(monkeypatch, recorded):
    monkeypatch.setenv("REVIEWER_BACKEND", "adk")
    _, pr, seen = _setup(monkeypatch)

    result = await agents.maintainer_node(_state())

    assert seen == {}  # the single-shot prompt was not sent
    (call,) = recorded
    assert call["pr"] is pr and "+x = 2" in call["diff"]
    assert result["merge_status"] == "awaiting_human_review"
    pr.create_issue_comment.assert_called_once()
    assert "APPROVE" in pr.create_issue_comment.call_args.args[0]


async def test_engine_config_selects_the_backend_and_ignores_the_env(
    monkeypatch, recorded
):
    monkeypatch.setenv("REVIEWER_BACKEND", "adk")
    _setup(monkeypatch)
    token = current_engine.set(engine_context("langgraph", monkeypatch))
    try:
        await agents.maintainer_node(_state())
    finally:
        current_engine.reset(token)
    assert recorded == []

    monkeypatch.delenv("REVIEWER_BACKEND")
    _setup(monkeypatch)
    token = current_engine.set(engine_context("adk", monkeypatch))
    try:
        await agents.maintainer_node(_state())
    finally:
        current_engine.reset(token)
    assert len(recorded) == 1


async def test_unknown_env_value_keeps_the_langgraph_review(monkeypatch, recorded):
    monkeypatch.setenv("REVIEWER_BACKEND", "crewai")
    _, _, seen = _setup(monkeypatch)
    await agents.maintainer_node(_state())
    assert recorded == [] and "system" in seen


async def test_env_adk_without_the_extra_aborts_the_run(monkeypatch):
    monkeypatch.setenv("REVIEWER_BACKEND", "adk")
    monkeypatch.setattr(config_module, "_adk_missing", lambda: "google.adk")
    _, pr, seen = _setup(monkeypatch)

    with pytest.raises(agents.PipelineAbortError) as raised:
        await agents.maintainer_node(_state())

    assert raised.value.error_code == "REVIEWER_BACKEND_UNAVAILABLE"
    assert "google.adk" in str(raised.value)
    assert seen == {}
    pr.create_issue_comment.assert_not_called()
    pr.merge.assert_not_called()


async def test_an_adk_rejection_requests_changes_and_iterates(monkeypatch):
    monkeypatch.setenv("REVIEWER_BACKEND", "adk")
    _, pr, _ = _setup(monkeypatch)

    async def reject(state, gh_repo, pr, diff_text):
        return agents.parse_decision(REJECT, agents.MaintainerDecision), None

    monkeypatch.setattr(reviewer_adk, "review_pull_request", reject)
    result = await agents.maintainer_node(_state())

    assert result["merge_status"] == "changes_requested"
    assert result["iteration"] == 1
    assert agents.parse_decision(
        result["review"], agents.MaintainerDecision
    ).verdict == ("reject")
    pr.merge.assert_not_called()


# --- the real ADK entry point, with a fake model and checkout ------------------------


@pytest.fixture
def head(tmp_path, monkeypatch):
    """``checkout_head`` replaced by a directory holding the PR head."""
    root = tmp_path / "head"
    root.mkdir()
    (root / "app.py").write_text("x = 2\n", encoding="utf-8")
    checkouts = []

    @asynccontextmanager
    async def checkout(clone_url, branch, head_sha):
        checkouts.append((branch, head_sha))
        yield Path(root)

    monkeypatch.setattr(reviewer_adk, "checkout_head", checkout)

    async def no_key(model_name):
        return None

    monkeypatch.setattr(reviewer_adk, "_acquire_key", no_key)
    return checkouts


@needs_adk
async def test_real_adk_review_through_the_node(monkeypatch, head):
    monkeypatch.setenv("REVIEWER_BACKEND", "adk")
    repo, pr, seen = _setup(monkeypatch)
    pr.head.ref = "automaintainer/run-1"
    factory = ModelFactory(
        policy=scripted(calls(("read_file", {"path": "app.py"})), APPROVE)
    )
    monkeypatch.setattr(reviewer_adk, "build_model", factory)

    result = await agents.maintainer_node(
        {**_state(), "verification": {"status": "passed", "exit_code": 0}}
    )

    assert seen == {}
    assert head == [("automaintainer/run-1", "headsha")]
    assert result["merge_status"] == "awaiting_human_review"
    assert agents.is_approved_verdict(result["review"])
    first = factory.turns[0]
    assert "#7 of o/r at head headsha" in first.user[0]
    assert "Targeted Issue #3" in first.user[0]
    assert "1| x = 2" in factory.turns[1].tool_results[0]


@pytest.mark.parametrize(
    "error",
    [
        reviewer_adk.CheckoutError("the branch moved since its head was read"),
        OSError("No space left on device"),
    ],
)
async def test_a_failed_checkout_is_a_rejection(monkeypatch, error):
    monkeypatch.setenv("REVIEWER_BACKEND", "adk")
    monkeypatch.setattr(config_module, "_adk_missing", lambda: None)
    _, pr, _ = _setup(monkeypatch)

    @asynccontextmanager
    async def broken(clone_url, branch, head_sha):
        raise error
        yield  # pragma: no cover

    monkeypatch.setattr(reviewer_adk, "checkout_head", broken)
    result = await agents.maintainer_node(_state())

    assert result["merge_status"] == "changes_requested"
    review = agents.parse_decision(result["review"], agents.MaintainerDecision)
    assert "could not be checked out" in review.reasons[0]
    pr.merge.assert_not_called()
