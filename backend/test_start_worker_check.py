"""/start answers 503 when no worker consumes agent_runs (#396)."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

import agents
import main
from main import app

client = TestClient(app)


class FakeInspect:
    def __init__(self, replies=None, error=None):
        self.replies = replies
        self.error = error
        self.calls = []

    def __call__(self, timeout):
        self.calls.append(timeout)
        return self

    def active_queues(self):
        if self.error:
            raise self.error
        return self.replies


@pytest.fixture(autouse=True)
def fresh_probe(monkeypatch):
    monkeypatch.delenv("AGENT_WORKER_CHECK", raising=False)
    monkeypatch.setattr(
        main, "_worker_probe", {"checked_at": float("-inf"), "available": False}
    )

    async def user():
        return {"user_id": "u1", "org_id": "org-1", "role": "member", "email": "e"}

    app.dependency_overrides[main.get_current_user] = user
    yield
    app.dependency_overrides.clear()


def use_inspect(monkeypatch, inspect):
    monkeypatch.setattr(
        main, "celery_app", SimpleNamespace(control=SimpleNamespace(inspect=inspect))
    )


def supabase_with_repo(monkeypatch):
    sb = MagicMock()
    repo_q = sb.table.return_value.select.return_value.eq.return_value.eq.return_value
    repo_q.limit.return_value.maybe_single.return_value.execute.return_value = (
        SimpleNamespace(data={"id": 1, "github_installation_id": 2})
    )
    monkeypatch.setattr(agents, "supabase", sb)
    return sb


def queued_task(monkeypatch):
    task = MagicMock()
    task.apply_async.return_value = SimpleNamespace(id="celery-1")
    monkeypatch.setattr(main, "run_agent_loop_task", task)
    return task


AGENT_WORKER = {"celery@a": [{"name": "agent_runs"}, {"name": "default"}]}
MAINTENANCE_WORKER = {"celery@m": [{"name": "maintenance"}, {"name": "github_sync"}]}


@pytest.mark.parametrize(
    "inspect",
    [
        FakeInspect(replies=None),  # no worker answered
        FakeInspect(replies=MAINTENANCE_WORKER),  # wrong queues only
        FakeInspect(error=ConnectionError("broker down")),
    ],
    ids=["no-reply", "maintenance-only", "broker-error"],
)
def test_start_without_agent_worker_is_503_and_creates_no_run(monkeypatch, inspect):
    sb = supabase_with_repo(monkeypatch)
    task = queued_task(monkeypatch)
    use_inspect(monkeypatch, inspect)

    response = client.post("/start", json={"repo_name": "o/r"})

    assert response.status_code == 503
    assert "agent worker" in response.json()["detail"]
    assert response.json()["code"] == "AGENT_WORKER_UNAVAILABLE"
    assert response.headers["Retry-After"]
    sb.table.return_value.insert.assert_not_called()
    task.apply_async.assert_not_called()


def test_start_with_agent_worker_queues_the_run(monkeypatch):
    sb = supabase_with_repo(monkeypatch)
    task = queued_task(monkeypatch)
    inspect = FakeInspect(replies={**MAINTENANCE_WORKER, **AGENT_WORKER})
    use_inspect(monkeypatch, inspect)

    response = client.post("/start", json={"repo_name": "o/r"})

    assert response.status_code == 200, response.text
    assert response.json()["celery_task_id"] == "celery-1"
    task.apply_async.assert_called_once()
    sb.table.return_value.insert.assert_called_once()
    assert inspect.calls == [main.WORKER_PING_TIMEOUT_SECONDS]


@pytest.mark.parametrize("value", ["false", "0", "off", " No "])
def test_disabled_check_queues_without_probing(monkeypatch, value):
    monkeypatch.setenv("AGENT_WORKER_CHECK", value)
    sb = supabase_with_repo(monkeypatch)
    task = queued_task(monkeypatch)
    inspect = FakeInspect(replies=None)  # would answer "no worker"
    use_inspect(monkeypatch, inspect)

    response = client.post("/start", json={"repo_name": "o/r"})

    assert response.status_code == 200, response.text
    assert inspect.calls == []
    sb.table.return_value.insert.assert_called_once()
    task.apply_async.assert_called_once()


@pytest.mark.parametrize("value", ["true", "1", "yes", ""])
def test_check_stays_on_for_other_values(monkeypatch, value):
    monkeypatch.setenv("AGENT_WORKER_CHECK", value)
    sb = supabase_with_repo(monkeypatch)
    queued_task(monkeypatch)
    use_inspect(monkeypatch, FakeInspect(replies=None))

    response = client.post("/start", json={"repo_name": "o/r"})

    assert response.status_code == 503
    sb.table.return_value.insert.assert_not_called()


def test_probe_result_is_cached_for_a_few_seconds(monkeypatch):
    clock = SimpleNamespace(now=100.0)
    monkeypatch.setattr(main.time, "monotonic", lambda: clock.now)
    inspect = FakeInspect(replies=None)
    use_inspect(monkeypatch, inspect)

    assert main._agent_worker_available_cached() is False
    inspect.replies = AGENT_WORKER
    clock.now += main.WORKER_PING_CACHE_SECONDS - 0.5
    assert main._agent_worker_available_cached() is False  # still cached
    assert len(inspect.calls) == 1

    clock.now += 1
    assert main._agent_worker_available_cached() is True
    assert len(inspect.calls) == 2
