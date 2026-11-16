"""Regression tests for #138 (failed repository clone) and #142 (failed fetch of
the targeted issue): both must end the run with a clear error code instead of
continuing on an empty working copy or an empty directive."""

import asyncio
import os
from unittest.mock import MagicMock

import pytest
from github import GithubException, RateLimitExceededException

import agents
import workspace
from events import BaseEvent, ErrorEvent, EventEmitter

TOKEN = "ghs_secretTokenValue123"


class Collector(EventEmitter):
    def __init__(self):
        self.events = []

    async def emit(self, event: BaseEvent) -> None:
        self.events.append(event)


def _errors(collector):
    return [e for e in collector.events if isinstance(e, ErrorEvent)]


@pytest.fixture
def offline(monkeypatch, tmp_path):
    """No Supabase, no real GitHub; the run's git token is TOKEN."""
    monkeypatch.setenv("AUTOMAINTAINER_WORKSPACE_DIR", str(tmp_path))
    monkeypatch.setattr(agents, "supabase", None)
    monkeypatch.setattr(agents, "_run_github_token", lambda: TOKEN)
    repo = MagicMock()
    repo.default_branch = "main"
    repo.get_contents.return_value = [MagicMock(path="app.py")]
    monkeypatch.setattr(agents, "gh", MagicMock(get_repo=MagicMock(return_value=repo)))
    llm_calls = []

    async def llm(*a, **k):
        llm_calls.append(a)
        return "Fix app.py:1-2"

    monkeypatch.setattr(agents, "run_llm_with_tools", llm)
    repo.llm_calls = llm_calls
    return repo


def _fake_git(monkeypatch, *, returncode=0, raises=None, hang=False):
    """Replace every subprocess with a fake git; returns the argv list seen."""
    seen = []

    async def fake_exec(*args, **kwargs):
        seen.append(args)
        if raises is not None:
            raise raises
        proc = MagicMock(returncode=returncode)

        async def communicate():
            # Only git clone hangs; later tools (gitnexus) must not.
            if hang and args[:2] == ("git", "clone"):
                await asyncio.sleep(3600)
            return (b"", b"fatal: could not read from remote repository")

        async def wait():
            return returncode

        proc.communicate = communicate
        proc.wait = wait
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    return seen


def _state():
    return {"repo_name": "o/r", "target_issue": None, "repository_id": 7}


def _repo_dir():
    return str(workspace.get_safe_repo_dir("o/r", 7))


def _assert_no_token(text):
    assert TOKEN not in text


# --- #138: a failed first clone ends the run ----------------------------------


@pytest.mark.asyncio
async def test_clone_nonzero_exit_aborts_architect(offline, monkeypatch, caplog):
    seen = _fake_git(monkeypatch, returncode=128)

    with pytest.raises(agents.PipelineAbortError) as exc:
        await agents.architect_node(_state())

    assert exc.value.error_code == "REPO_CLONE_FAILED"
    assert exc.value.phase == agents.AgentPhase.ARCHITECT
    assert seen and seen[0][:2] == ("git", "clone")
    # The token never reaches git's argv or any message.
    assert all(TOKEN not in str(a) for a in seen)
    _assert_no_token(str(exc.value))
    _assert_no_token(caplog.text)
    # No LLM work on an empty codebase, and no partial directory left behind.
    assert offline.llm_calls == []
    assert not os.path.exists(_repo_dir())


@pytest.mark.asyncio
async def test_clone_exception_aborts_and_redacts_credentials(
    offline, monkeypatch, caplog
):
    _fake_git(
        monkeypatch,
        raises=OSError(
            f"cannot run git for https://x-access-token:{TOKEN}@github.com/o/r.git"
        ),
    )

    with pytest.raises(agents.PipelineAbortError) as exc:
        await agents.architect_node(_state())

    assert exc.value.error_code == "REPO_CLONE_FAILED"
    message = str(exc.value)
    assert "OSError" in message
    assert "https://***@github.com/o/r.git" in message
    _assert_no_token(message)
    _assert_no_token(caplog.text)
    assert offline.llm_calls == []


@pytest.mark.asyncio
async def test_clone_timeout_aborts(offline, monkeypatch):
    _fake_git(monkeypatch, returncode=-9, hang=True)
    real_sync = workspace.sync_repo_clone

    async def fast_timeout(*a, **k):
        return await real_sync(*a, **k, timeout=0.05)

    monkeypatch.setattr(workspace, "sync_repo_clone", fast_timeout)

    with pytest.raises(agents.PipelineAbortError) as exc:
        await agents.architect_node(_state())

    assert exc.value.error_code == "REPO_CLONE_FAILED"
    assert "timed out" in str(exc.value)
    assert offline.llm_calls == []


@pytest.mark.asyncio
async def test_clone_failure_fails_the_whole_run(offline, monkeypatch):
    _fake_git(monkeypatch, returncode=128)
    collector = Collector()

    result = await agents.run_agent_loop("o/r", run_id="r138", emitter=collector)

    assert result["status"] == "failed"
    assert result["error_code"] == "REPO_CLONE_FAILED"
    _assert_no_token(result["error"])
    fatal = [e for e in _errors(collector) if e.fatal]
    assert [e.error_code for e in fatal] == ["REPO_CLONE_FAILED"]
    offline.create_issue.assert_not_called()
    offline.create_pull.assert_not_called()
    assert offline.llm_calls == []


@pytest.mark.asyncio
async def test_failed_update_of_cached_clone_continues_visibly(offline, monkeypatch):
    # An earlier run left a real working copy; only the fetch fails now.
    os.makedirs(os.path.join(_repo_dir(), ".git"))
    _fake_git(monkeypatch, returncode=1)
    collector = Collector()
    tokens = (
        agents.current_run_id.set("r-sync"),
        agents.current_emitter.set(collector),
    )
    try:
        result = await agents.architect_node(_state())
    finally:
        agents.current_emitter.reset(tokens[1])
        agents.current_run_id.reset(tokens[0])

    assert result["architect_directive"] == "Fix app.py:1-2"
    warnings = [e for e in _errors(collector) if e.error_code == "REPO_SYNC_FAILED"]
    assert len(warnings) == 1 and not warnings[0].fatal
    _assert_no_token(warnings[0].message)
    assert any("cached copy" in m.get("msg", "") for m in result["log_messages"])
    # The cache is kept, not deleted.
    assert os.path.isdir(os.path.join(_repo_dir(), ".git"))


def test_redact_credentials_masks_token_and_url_userinfo():
    text = f"x {TOKEN} https://user:pw@github.com/o/r.git http://a@h/p"
    out = agents._redact_credentials(text, TOKEN)
    assert out == "x *** https://***@github.com/o/r.git http://***@h/p"
    assert agents._redact_credentials("plain", None) == "plain"


# --- #142: a failed fetch of the targeted issue ends the run ------------------


def _target_state(number=42):
    return {"repo_name": "o/r", "target_issue": number}


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [404, 410])
async def test_missing_target_issue_is_invalid(offline, status):
    offline.get_issue.side_effect = GithubException(status, {"message": "x"}, None)

    with pytest.raises(agents.PipelineAbortError) as exc:
        await agents.architect_node(_target_state())

    assert exc.value.error_code == "INVALID_TARGET_ISSUE"
    assert "#42" in str(exc.value)
    assert offline.llm_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        GithubException(502, {"message": "Bad Gateway"}, None),
        RateLimitExceededException(403, {"message": "API rate limit exceeded"}, None),
    ],
    ids=["5xx", "rate-limit"],
)
async def test_transient_issue_fetch_failure_fails_run(offline, error):
    offline.get_issue.side_effect = error
    collector = Collector()

    result = await agents.run_agent_loop(
        "o/r", target_issue=42, run_id="r142", emitter=collector
    )

    assert result["status"] == "failed"
    assert result["error_code"] == "FETCH_ISSUE_FAILED"
    assert f"returned {error.status}" in result["error"]
    # Never proceeds with an empty directive.
    assert offline.llm_calls == []
    offline.create_issue.assert_not_called()
    offline.create_git_ref.assert_not_called()
    fatal = [e for e in _errors(collector) if e.fatal]
    assert [e.error_code for e in fatal] == ["FETCH_ISSUE_FAILED"]


@pytest.mark.asyncio
async def test_non_github_issue_fetch_error_fails_run(offline):
    offline.get_issue.side_effect = ConnectionError("connection reset")

    with pytest.raises(agents.PipelineAbortError) as exc:
        await agents.architect_node(_target_state())

    assert exc.value.error_code == "FETCH_ISSUE_FAILED"
    assert offline.llm_calls == []
