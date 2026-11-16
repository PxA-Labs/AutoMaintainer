"""Regression tests: LLM failures and node crashes end the run instead of
leaking error strings into GitHub, and the loop has no artificial delay."""

import asyncio
import sys
from unittest.mock import MagicMock

import pytest

import agents
from events import BaseEvent, ErrorEvent, EventEmitter


class Collector(EventEmitter):
    def __init__(self):
        self.events = []

    async def emit(self, event: BaseEvent) -> None:
        self.events.append(event)


def _fatal_errors(collector):
    return [e for e in collector.events if isinstance(e, ErrorEvent) and e.fatal]


@pytest.fixture
def offline(monkeypatch, tmp_path):
    """No Supabase, no real git subprocesses, no real GitHub."""
    monkeypatch.setenv("AUTOMAINTAINER_WORKSPACE_DIR", str(tmp_path))
    monkeypatch.setattr(agents, "supabase", None)

    async def fake_exec(*a, **k):
        p = MagicMock(returncode=0)

        async def communicate():
            return (b"", b"")

        p.communicate = communicate
        return p

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    repo = MagicMock()
    repo.default_branch = "main"
    repo.get_contents.return_value = [MagicMock(path="app.py")]
    monkeypatch.setattr(agents, "gh", MagicMock(get_repo=MagicMock(return_value=repo)))
    return repo


@pytest.mark.asyncio
async def test_run_llm_with_tools_raises_typed_error_instead_of_error_string(
    monkeypatch,
):
    # Force the MCP path to fail at import time and the plain fallback to fail too.
    monkeypatch.setitem(sys.modules, "mcp.client.stdio", None)

    async def failing_llm(*a, **k):
        raise RuntimeError("all keys exhausted")

    monkeypatch.setattr(agents, "run_llm", failing_llm)
    with pytest.raises(agents.LLMExecutionError, match="all keys exhausted"):
        await agents.run_llm_with_tools("sys", "user")


@pytest.mark.asyncio
async def test_brainstormer_llm_failure_does_not_create_issue(offline, monkeypatch):
    async def failing(*a, **k):
        raise agents.LLMExecutionError("LLM execution failed: 429")

    monkeypatch.setattr(agents, "run_llm_with_tools", failing)
    with pytest.raises(agents.PipelineAbortError) as exc:
        await agents.brainstormer_node(
            {"repo_name": "o/r", "architect_directive": "Fix app.py:1-2"}
        )
    assert exc.value.error_code == "LLM_EXECUTION_FAILED"
    offline.create_issue.assert_not_called()


@pytest.mark.asyncio
async def test_llm_outage_fails_run_without_publishing_error_text(offline, monkeypatch):
    async def failing(*a, **k):
        raise agents.LLMExecutionError("LLM execution failed: 429")

    monkeypatch.setattr(agents, "run_llm_with_tools", failing)
    collector = Collector()
    result = await agents.run_agent_loop("o/r", run_id="r1", emitter=collector)

    assert result["status"] == "failed"
    assert result["error_code"] == "LLM_EXECUTION_FAILED"
    offline.create_issue.assert_not_called()
    offline.create_pull.assert_not_called()
    assert [e.error_code for e in _fatal_errors(collector)] == ["LLM_EXECUTION_FAILED"]


@pytest.mark.asyncio
async def test_unexpected_node_exception_emits_fatal_error_event(monkeypatch):
    monkeypatch.setattr(agents, "supabase", None)

    class ExplodingApp:
        async def astream(self, *a, **k):
            raise ValueError("boom")
            yield  # pragma: no cover - makes this an async generator

    monkeypatch.setattr(agents, "app", ExplodingApp())
    collector = Collector()
    result = await agents.run_agent_loop("o/r", run_id="r2", emitter=collector)

    assert result["status"] == "failed"
    assert "boom" in result["error"]
    fatal = _fatal_errors(collector)
    assert len(fatal) == 1 and fatal[0].error_code == "RUN_FAILED"


@pytest.mark.asyncio
async def test_log_messages_are_not_throttled_with_sleep(monkeypatch):
    monkeypatch.setattr(agents, "supabase", None)
    msgs = [{"agent": "A", "msg": str(i)} for i in range(5)]

    class FakeApp:
        async def astream(self, *a, **k):
            yield {"log_messages": msgs, "review": "", "issue_number": 0}

    monkeypatch.setattr(agents, "app", FakeApp())
    sleeps = []
    real_sleep = asyncio.sleep

    async def spy_sleep(delay, *a, **k):
        sleeps.append(delay)
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", spy_sleep)
    result = await agents.run_agent_loop("o/r", run_id="r3", emitter=Collector())
    assert result["status"] == "completed"
    assert 0.5 not in sleeps
