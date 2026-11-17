"""/start answers 503 when no worker consumes agent_runs (#396)."""

import asyncio
import concurrent.futures
import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

import agents
import main
from main import app

client = TestClient(app)


class FakeInspect:
    def __init__(self, replies=None, error=None, hang=None):
        self.replies = replies
        self.error = error
        self.hang = hang  # a threading.Event the probe blocks on
        self.calls = []

    def __call__(self, timeout, connection=None):
        self.calls.append(timeout)
        return self

    def active_queues(self):
        if self.hang:
            self.hang.wait(10)
        if self.error:
            raise self.error
        return self.replies


class FakeConnection:
    def __init__(self, transport_options=None):
        self.transport_options = transport_options
        self.ensure_calls = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def ensure_connection(self, **kwargs):
        self.ensure_calls.append(kwargs)


@pytest.fixture(autouse=True)
def fresh_probe(monkeypatch):
    monkeypatch.delenv("AGENT_WORKER_CHECK", raising=False)
    monkeypatch.setattr(main, "_worker_check_off_warned", False)
    monkeypatch.setattr(
        main,
        "_worker_probe",
        {"checked_at": float("-inf"), "available": False, "future": None},
    )
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    monkeypatch.setattr(main, "_worker_probe_executor", executor)

    async def user():
        return {"user_id": "u1", "org_id": "org-1", "role": "member", "email": "e"}

    app.dependency_overrides[main.get_current_user] = user
    yield
    app.dependency_overrides.clear()
    executor.shutdown(wait=True)


def use_inspect(monkeypatch, inspect):
    connections = []

    def connection_for_write(**kwargs):
        connections.append(FakeConnection(**kwargs))
        return connections[-1]

    monkeypatch.setattr(
        main,
        "celery_app",
        SimpleNamespace(
            control=SimpleNamespace(inspect=inspect),
            connection_for_write=connection_for_write,
        ),
    )
    return connections


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

    def available():
        return asyncio.run(main.agent_worker_available())

    assert available() is False
    inspect.replies = AGENT_WORKER
    clock.now += main.WORKER_PING_CACHE_SECONDS - 0.5
    assert available() is False  # still cached
    assert len(inspect.calls) == 1

    clock.now += 1
    assert available() is True
    assert len(inspect.calls) == 2


def test_probe_connects_with_short_timeouts_and_one_retry(monkeypatch):
    connections = use_inspect(monkeypatch, FakeInspect(replies=AGENT_WORKER))

    assert asyncio.run(main.agent_worker_available()) is True

    timeout = main.WORKER_PING_TIMEOUT_SECONDS
    (conn,) = connections
    assert conn.transport_options == {
        "socket_connect_timeout": timeout,
        "socket_timeout": timeout,
    }
    assert conn.ensure_calls == [{"max_retries": 1, "timeout": timeout}]


def test_hung_probe_answers_503_by_the_deadline_and_blocks_no_one(monkeypatch):
    # A blackholed broker host: the probe blocks far past its timeout.
    monkeypatch.setattr(main, "WORKER_PROBE_DEADLINE_SECONDS", 0.3)
    sb = supabase_with_repo(monkeypatch)
    task = queued_task(monkeypatch)
    release = threading.Event()
    inspect = FakeInspect(replies=AGENT_WORKER, hang=release)
    use_inspect(monkeypatch, inspect)
    try:
        started = time.monotonic()
        first = client.post("/start", json={"repo_name": "o/r"})
        first_took = time.monotonic() - started

        started = time.monotonic()
        second = client.post("/start", json={"repo_name": "o/r"})
        second_took = time.monotonic() - started
    finally:
        release.set()

    assert first.status_code == 503
    assert first.json()["code"] == "AGENT_WORKER_UNAVAILABLE"
    assert first_took < 2
    # The stuck probe is past its deadline: no second wait, no second probe.
    assert second.status_code == 503
    assert second_took < 0.25
    assert len(inspect.calls) == 1
    sb.table.return_value.insert.assert_not_called()
    task.apply_async.assert_not_called()


def test_probe_failure_log_and_body_leave_out_broker_credentials(monkeypatch, caplog):
    supabase_with_repo(monkeypatch)
    queued_task(monkeypatch)
    error = ConnectionError("Error connecting to redis://user:s3cret@host:6379/0")
    use_inspect(monkeypatch, FakeInspect(error=error))

    with caplog.at_level("WARNING", logger=main.logger.name):
        response = client.post("/start", json={"repo_name": "o/r"})

    assert response.status_code == 503
    assert "s3cret" not in response.text
    assert "redis://" not in response.text
    logged = [r.getMessage() for r in caplog.records if "probe failed" in r.message]
    assert logged == [
        "Celery worker probe failed: Error connecting to redis://***@host:6379/0"
    ]


@pytest.mark.parametrize(
    "environment, warned", [("production", True), ("development", False)]
)
def test_disabled_check_warns_once_in_production(
    monkeypatch, caplog, environment, warned
):
    monkeypatch.setenv("AGENT_WORKER_CHECK", "false")
    monkeypatch.setenv("ENVIRONMENT", environment)
    supabase_with_repo(monkeypatch)
    queued_task(monkeypatch)
    use_inspect(monkeypatch, FakeInspect(replies=None))

    with caplog.at_level("WARNING", logger=main.logger.name):
        for _ in range(2):
            assert client.post("/start", json={"repo_name": "o/r"}).status_code == 200

    warnings = [r for r in caplog.records if "AGENT_WORKER_CHECK is off" in r.message]
    assert len(warnings) == (1 if warned else 0)
