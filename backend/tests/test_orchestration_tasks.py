"""
Orchestration reliability tests for tasks.py / redis_locks.py / celery_app.py.

Covers: no blanket Celery autoretry (no duplicate side effects, timeouts are
terminal), idempotent redelivery, re-entrant repo lock, and stale-run
cleanup that respects heartbeats.

No real Redis, Celery broker, Supabase or GitHub is used. Each task
execution creates its own event loop, so running a task twice here also
exercises the "fresh loop per delivery" path a Celery worker takes.
"""

import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import redis  # noqa: E402

import redis_locks  # noqa: E402
import tasks  # noqa: E402

RUN_KWARGS = dict(
    run_id="run-1",
    repo_name="org/repo",
    org_id="org-1",
    user_id="user-1",
    repository_id=42,
    github_installation_id=99,
)


class _Retry(Exception):
    """Stands in for celery.exceptions.Retry."""


class FakeTask:
    """Minimal bound-task stand-in: request info plus a recording retry()."""

    def __init__(self, retries=0):
        self.request = SimpleNamespace(id="celery-task-1", retries=retries)
        self.retry_calls = []

    def retry(self, **kwargs):
        self.retry_calls.append(kwargs)
        return _Retry(kwargs)


class FakeRedis:
    """In-memory Redis with real semantics for the scripts redis_locks uses."""

    def __init__(self):
        self.store = {}

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.store:
            return None
        self.store[key] = value
        return True

    def eval(self, script, numkeys, key, value):
        owned = self.store.get(key) == value
        if script == redis_locks._RELEASE_SCRIPT:
            if owned:
                del self.store[key]
                return 1
            return 0
        if script == redis_locks._REENTER_SCRIPT:
            return 1 if owned else 0
        raise AssertionError("unexpected script")

    def exists(self, key):
        return int(key in self.store)


@pytest.fixture
def fake_run(monkeypatch):
    """Patch DB/Redis/agent dependencies; return a handle to inspect calls."""
    state = SimpleNamespace(
        statuses={"run-1": "queued"},
        updates=[],
        agent_calls=[],
        heartbeats=[],
        agent_result={"status": "completed", "summary": "ok"},
        agent_exc=None,
    )
    fake_redis = FakeRedis()
    monkeypatch.setattr(redis_locks, "_redis_client", fake_redis)
    state.redis = fake_redis

    monkeypatch.setattr(tasks, "_fetch_run_status", lambda rid: state.statuses.get(rid))

    async def fake_update(run_id, status, **kwargs):
        state.statuses[run_id] = status
        state.updates.append((run_id, status, kwargs))

    async def noop(*a, **k):
        return None

    async def fake_agent_loop(repo_name, target_issue=None, run_id=None, **kwargs):
        state.agent_calls.append(
            dict(
                repo_name=repo_name, target_issue=target_issue, run_id=run_id, **kwargs
            )
        )
        if state.agent_exc is not None:
            raise state.agent_exc
        return state.agent_result

    monkeypatch.setattr(tasks, "update_run_status", fake_update)
    monkeypatch.setattr(tasks, "record_usage_event", noop)
    monkeypatch.setattr(tasks, "log_run_event", noop)
    monkeypatch.setattr(tasks, "run_agent_loop", fake_agent_loop)
    monkeypatch.setattr(
        tasks,
        "write_heartbeat",
        lambda run_id, ttl_seconds=60: state.heartbeats.append((run_id, ttl_seconds)),
    )
    return state


# --- autoretry / side effects ------------------------------------------------


def test_task_has_no_blanket_autoretry():
    # Only meaningful with real Celery installed (as in CI); the ImportError
    # fallback shim has no retry machinery at all.
    autoretry = tuple(getattr(tasks.run_agent_loop_task, "autoretry_for", ()) or ())
    assert Exception not in autoretry
    assert not issubclass(tasks.SoftTimeLimitExceeded, autoretry)


def test_failure_after_side_effects_is_not_retried(fake_run):
    # E.g. GitHub create_pull returning 502 after the issue/branch exist.
    fake_run.agent_exc = RuntimeError("502 Bad Gateway from GitHub")
    task = FakeTask()

    with pytest.raises(RuntimeError):
        tasks._execute_agent_run(task, **RUN_KWARGS)

    assert task.retry_calls == []
    assert len(fake_run.agent_calls) == 1
    assert fake_run.statuses["run-1"] == "failed"
    # Lock released so the next run for this repo isn't blocked.
    assert fake_run.redis.store == {}


def test_transient_redis_error_after_side_effects_is_not_retried(fake_run):
    fake_run.agent_exc = redis.exceptions.ConnectionError("redis went away")
    task = FakeTask()

    with pytest.raises(redis.exceptions.ConnectionError):
        tasks._execute_agent_run(task, **RUN_KWARGS)

    assert task.retry_calls == []
    assert fake_run.statuses["run-1"] == "failed"


def test_soft_time_limit_is_terminal(fake_run):
    fake_run.agent_exc = tasks.SoftTimeLimitExceeded()
    task = FakeTask()

    with pytest.raises(tasks.SoftTimeLimitExceeded):
        tasks._execute_agent_run(task, **RUN_KWARGS)

    assert task.retry_calls == []
    assert fake_run.statuses["run-1"] == "failed"


def test_transient_error_before_side_effects_is_retried(fake_run, monkeypatch):
    err = redis.exceptions.ConnectionError("connection refused")

    def boom(*a, **k):
        raise err

    monkeypatch.setattr(tasks, "acquire_repo_lock", boom)
    task = FakeTask()

    with pytest.raises(_Retry):
        tasks._execute_agent_run(task, **RUN_KWARGS)

    assert len(task.retry_calls) == 1
    assert task.retry_calls[0]["exc"] is err
    assert fake_run.agent_calls == []
    assert fake_run.updates == []


# --- redelivery idempotency ---------------------------------------------------


def test_redelivery_after_completion_is_a_noop(fake_run):
    first = tasks._execute_agent_run(FakeTask(), **RUN_KWARGS)
    assert first["status"] == "completed"

    # Same message delivered again (acks_late + worker loss), on a new loop.
    second = tasks._execute_agent_run(FakeTask(), **RUN_KWARGS)

    assert second == {"status": "completed", "skipped": True}
    assert len(fake_run.agent_calls) == 1


def test_run_stopped_by_its_engine_stays_cancelled(fake_run):
    # The engine's cancel (#186) ends run_agent_loop with status "cancelled";
    # the task must not record that as a failure.
    fake_run.agent_result = {
        "status": "cancelled",
        "error": "Agent loop cancelled by user.",
        "error_code": "RUN_CANCELLED",
    }

    result = tasks._execute_agent_run(FakeTask(), **RUN_KWARGS)

    assert result["status"] == "cancelled"
    assert fake_run.statuses["run-1"] == "cancelled"
    assert "failed" not in [status for _, status, _ in fake_run.updates]
    assert fake_run.redis.store == {}  # repo lock released


@pytest.mark.parametrize("status", ["completed", "failed", "cancelled"])
def test_terminal_run_is_skipped_without_taking_lock(fake_run, status):
    fake_run.statuses["run-1"] = status

    result = tasks._execute_agent_run(FakeTask(), **RUN_KWARGS)

    assert result == {"status": status, "skipped": True}
    assert fake_run.agent_calls == []
    assert fake_run.redis.store == {}


def test_redelivery_reacquires_lock_held_by_same_run(fake_run):
    # A previous delivery of run-1 died while holding the repo lock.
    assert redis_locks.acquire_repo_lock(42, "run-1") is True
    fake_run.statuses["run-1"] = "running"
    task = FakeTask()

    result = tasks._execute_agent_run(task, **RUN_KWARGS)

    assert result["status"] == "completed"
    assert task.retry_calls == []
    assert len(fake_run.agent_calls) == 1


def test_lock_is_reentrant_only_for_its_owner(fake_run):
    assert redis_locks.acquire_repo_lock(42, "run-1") is True
    assert redis_locks.acquire_repo_lock(42, "run-1") is True
    assert redis_locks.acquire_repo_lock(42, "run-2") is False


def test_busy_lock_waits_with_heartbeat(fake_run):
    redis_locks.acquire_repo_lock(42, "other-run")
    task = FakeTask()

    with pytest.raises(_Retry):
        tasks._execute_agent_run(task, **RUN_KWARGS)

    assert task.retry_calls[0]["countdown"] == tasks.LOCK_WAIT_COUNTDOWN
    assert ("run-1", tasks.LOCK_WAIT_HEARTBEAT_TTL) in fake_run.heartbeats
    assert fake_run.agent_calls == []


def test_busy_lock_exhausted_marks_run_failed(fake_run):
    redis_locks.acquire_repo_lock(42, "other-run")
    task = FakeTask(retries=tasks.LOCK_WAIT_MAX_RETRIES)

    result = tasks._execute_agent_run(task, **RUN_KWARGS)

    assert result["status"] == "failed"
    assert fake_run.statuses["run-1"] == "failed"
    assert task.retry_calls == []
    # The other run's lock is untouched.
    assert fake_run.redis.store == {redis_locks._lock_key(42): "other-run"}


# --- github_installation_id pass-through ---------------------------------------


def test_installation_id_passed_when_supported(fake_run):
    tasks._execute_agent_run(FakeTask(), **RUN_KWARGS)
    assert fake_run.agent_calls[0]["github_installation_id"] == 99


def test_installation_id_dropped_for_legacy_signature(fake_run, monkeypatch):
    calls = []

    async def legacy_loop(repo_name, target_issue=None, run_id=None):
        calls.append(run_id)
        return {"status": "completed"}

    monkeypatch.setattr(tasks, "run_agent_loop", legacy_loop)

    result = tasks._execute_agent_run(FakeTask(), **RUN_KWARGS)

    assert result["status"] == "completed"
    assert calls == ["run-1"]


# --- cleanup_stale_runs -------------------------------------------------------


class _Query:
    def __init__(self, rows):
        self.rows = rows

    def __getattr__(self, name):
        return lambda *a, **k: self

    def execute(self):
        return SimpleNamespace(data=self.rows)


def _call_cleanup():
    task = tasks.cleanup_stale_runs
    # Real Celery: the task object binds `self`; fallback shim: plain function.
    if tasks.CELERY_AVAILABLE:
        return task()
    return task(SimpleNamespace())


@pytest.fixture
def cleanup_env(monkeypatch, fake_run):
    rows = [
        {
            "id": "live-run",
            "org_id": "o",
            "repo_name": "org/live",
            "repository_id": 1,
            "celery_task_id": "t-live",
        },
        {
            "id": "dead-run",
            "org_id": "o",
            "repo_name": "org/dead",
            "repository_id": 2,
            "celery_task_id": "t-dead",
        },
    ]
    sb = MagicMock()
    sb.table.side_effect = lambda name: _Query(rows)
    monkeypatch.setattr(tasks, "get_supabase", lambda: sb)
    monkeypatch.setattr(tasks, "is_heartbeat_alive", lambda rid: rid == "live-run")

    import celery_app as celery_app_module

    control = MagicMock()
    monkeypatch.setattr(celery_app_module.celery_app, "control", control)

    redis_locks.acquire_repo_lock(1, "live-run")
    redis_locks.acquire_repo_lock(2, "dead-run")
    fake_run.control = control
    return fake_run


def test_cleanup_skips_runs_with_live_heartbeat(cleanup_env):
    result = _call_cleanup()

    assert result["run_ids"] == ["dead-run"]
    assert "live-run" not in cleanup_env.statuses
    assert cleanup_env.statuses["dead-run"] == "failed"
    # The live run keeps its lock.
    assert cleanup_env.redis.store.get(redis_locks._lock_key(1)) == "live-run"


def test_cleanup_revokes_task_and_releases_lock(cleanup_env):
    _call_cleanup()

    cleanup_env.control.revoke.assert_called_once_with(
        "t-dead", terminate=True, signal="SIGTERM"
    )
    assert redis_locks._lock_key(2) not in cleanup_env.redis.store
