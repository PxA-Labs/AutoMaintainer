"""GitHub-backed repo routes must not block the event loop (#404).

PyGithub is synchronous: every call is a blocking HTTP round trip. When the
async handlers called it directly, one slow GitHub request stalled every other
request, WebSocket and SSE stream on the worker. These tests mock PyGithub with
calls that sleep (blocking, like real network I/O) and drive the app on a real
event loop.
"""

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from github import UnknownObjectException

import agents
import main
from main import app

DELAY = 0.6  # seconds each mocked GitHub write blocks for


def _slow(result=None):
    def call(*_args, **_kwargs):
        time.sleep(DELAY)  # deliberately blocking, like PyGithub
        return result

    return call


@pytest.fixture
def slow_github(monkeypatch, tmp_path):
    monkeypatch.setenv("AUTOMAINTAINER_WORKSPACE_DIR", str(tmp_path))
    monkeypatch.setattr(agents, "supabase", MagicMock())
    monkeypatch.setattr(main, "validate_repo_access", AsyncMock(return_value=(1, 1)))

    repo = MagicMock()
    repo.default_branch = "main"
    repo.get_contents.return_value = SimpleNamespace(path="a.py", sha="s")
    # IDE writes go to an unprotected working branch, never "main" (#139).
    repo.get_branch.return_value = SimpleNamespace(protected=False)
    repo.update_file.side_effect = _slow()
    repo.delete_file.side_effect = _slow()
    repo.create_file.side_effect = _slow()
    repo.get_git_ref.return_value = SimpleNamespace(object=SimpleNamespace(sha="b"))
    repo.create_pull.side_effect = _slow(SimpleNamespace(number=7, html_url="u"))
    gh = MagicMock(get_repo=lambda _name: repo)

    async def client_for(_repo_name, _org_id):
        return gh

    monkeypatch.setattr(main, "_github_client_for_repo", client_for)

    overrides = {
        main.require_repo_write_access: lambda: "org-a",
        main.get_org_id: lambda: "org-a",
        main.get_user_id: lambda: "u",
        main.require_write_role: lambda: "member",
    }
    app.dependency_overrides.update(overrides)
    yield repo
    for dep in overrides:
        app.dependency_overrides.pop(dep, None)


def _client():
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


@pytest.mark.asyncio
async def test_two_slow_file_updates_run_concurrently(slow_github):
    async with _client() as client:
        start = time.perf_counter()
        r1, r2 = await asyncio.gather(
            client.post("/repo/a/app/file", json={"file_path": "a.py", "content": "1"}),
            client.post("/repo/a/app/file", json={"file_path": "b.py", "content": "2"}),
        )
        elapsed = time.perf_counter() - start

    assert r1.status_code == r2.status_code == 200
    assert slow_github.update_file.call_count == 2
    # Serialized on the event loop this takes ~2 * DELAY.
    assert elapsed < 1.6 * DELAY, f"requests were serialized ({elapsed:.2f}s)"


async def _health_latency_during(client, slow_request):
    """Time from starting the slow request until /healthz has answered.

    Measured from the start, not from sending /healthz: a blocked loop also
    delays the client's own ``asyncio.sleep`` wake-up, which would hide the
    stall from a timer started afterwards.
    """
    start = time.perf_counter()
    task = asyncio.create_task(slow_request)
    await asyncio.sleep(DELAY / 6)  # let the slow call get under way
    health = await client.get("/healthz")
    latency = time.perf_counter() - start
    response = await task
    return health, latency, response


@pytest.mark.parametrize(
    "method,path,kwargs",
    [
        ("post", "/repo/a/app/file", {"json": {"file_path": "a.py", "content": "x"}}),
        (
            "post",
            "/repo/a/app/file/create",
            {"json": {"file_path": "n.py", "content": "x"}},
        ),
        ("delete", "/repo/a/app/file?file_path=a.py", {}),
        (
            "post",
            "/repo/a/app/propose-changes",
            {"json": {"title": "t", "changes": [{"path": "a.py", "content": "x"}]}},
        ),
    ],
)
@pytest.mark.asyncio
async def test_healthz_stays_responsive_during_slow_github_call(
    slow_github, method, path, kwargs
):
    async with _client() as client:
        slow = getattr(client, method)(path, **kwargs)
        health, latency, response = await _health_latency_during(client, slow)

    assert response.status_code == 200, response.text
    assert health.status_code == 200
    assert latency < DELAY / 2, f"/healthz waited {latency:.2f}s on GitHub"


@pytest.mark.asyncio
async def test_errors_raised_in_the_worker_thread_still_map_to_http(slow_github):
    slow_github.get_contents.side_effect = UnknownObjectException(
        404, {"message": "Not Found"}, None
    )
    async with _client() as client:
        r = await client.post(
            "/repo/a/app/file", json={"file_path": "a.py", "content": "x"}
        )
    assert r.status_code == 404
    assert r.json()["detail"] == "File not found in repo"


@pytest.mark.asyncio
async def test_non_404_github_errors_are_not_reported_as_missing(slow_github):
    slow_github.get_contents.side_effect = Exception("GitHub 500")
    async with _client() as client:
        r = await client.post(
            "/repo/a/app/file", json={"file_path": "a.py", "content": "x"}
        )
    assert r.status_code == 502
