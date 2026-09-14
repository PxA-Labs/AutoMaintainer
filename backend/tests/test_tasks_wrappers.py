"""
Basic unit tests for start_agent_task() / cancel_agent_task() in tasks.py.

No real Redis, Celery broker, or FastAPI server needed -- is_redis_available,
run_agent_loop_task, and cancel_run_task are all monkeypatched, so this only
tests the branching logic (queue vs. inline fallback, delegation), not the
actual infrastructure calls.

Assumes tasks.py already imports cleanly in your environment (i.e. the
supabase package and agents.py are present, same as for running the app).

Run with: pytest tests/test_tasks_wrappers.py -v
Needs only: pip install pytest
"""

import os
import sys
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import tasks

SAMPLE_PAYLOAD = {
    "repo_name": "org/repo",
    "org_id": "org-1",
    "user_id": "user-1",
    "repository_id": 42,
    "github_installation_id": 99,
}


def test_start_agent_task_queues_when_redis_available(monkeypatch):
    monkeypatch.setattr(tasks, "is_redis_available", lambda: True)
    monkeypatch.setattr(tasks, "CELERY_AVAILABLE", True)

    fake_async_result = MagicMock(id="celery-task-123")
    monkeypatch.setattr(
        tasks.run_agent_loop_task, "delay", MagicMock(return_value=fake_async_result)
    )

    outcome = tasks.start_agent_task("run-1", SAMPLE_PAYLOAD)

    assert outcome == {"success": True, "queued": True, "task_id": "celery-task-123"}
    tasks.run_agent_loop_task.delay.assert_called_once()


def test_start_agent_task_falls_back_when_redis_unavailable(monkeypatch):
    monkeypatch.setattr(tasks, "is_redis_available", lambda: False)

    received_kwargs = {}

    def fake_direct_call(**kwargs):
        received_kwargs.update(kwargs)
        return {"status": "completed"}

    # Replaces the task entirely -- proves the fallback path calls the
    # function directly (in-process) rather than going through .delay().
    monkeypatch.setattr(tasks, "run_agent_loop_task", fake_direct_call)

    outcome = tasks.start_agent_task("run-2", SAMPLE_PAYLOAD)

    assert outcome == {
        "success": True,
        "queued": False,
        "result": {"status": "completed"},
    }
    assert received_kwargs["run_id"] == "run-2"
    assert received_kwargs["repository_id"] == 42


def test_cancel_agent_task_delegates_to_cancel_run_task(monkeypatch):
    sentinel = {"success": True}
    monkeypatch.setattr(tasks, "cancel_run_task", lambda run_id: sentinel)

    assert tasks.cancel_agent_task("run-3") is sentinel