"""POST /start records its trigger and answers 409 for an issue that already
has an active run (#518).

The database is mocked: the runs_one_active_per_issue index is represented
by the error postgrest-py raises when an insert violates it.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient
from postgrest.exceptions import APIError

import agents
import main
from main import app

client = TestClient(app)

USER = {
    "user_id": "u1",
    "org_id": "org-1",
    "role": "member",
    "email": "e",
    "github_id": 4242,
    "github_login": "octo",
}


@pytest.fixture
def start(monkeypatch):
    """/start with a registered repository, a worker and a mocked Celery task."""

    async def user():
        return dict(USER)

    app.dependency_overrides[main.get_current_user] = user
    monkeypatch.setattr(main, "agent_worker_available", AsyncMock(return_value=True))
    sb = MagicMock()
    repo_q = sb.table.return_value.select.return_value.eq.return_value.eq.return_value
    repo_q.limit.return_value.maybe_single.return_value.execute.return_value = (
        SimpleNamespace(data={"id": 1, "github_installation_id": 2})
    )
    monkeypatch.setattr(agents, "supabase", sb)
    task = MagicMock()
    task.apply_async.return_value = SimpleNamespace(id="celery-1")
    monkeypatch.setattr(main, "run_agent_loop_task", task)
    yield SimpleNamespace(sb=sb, task=task, insert=sb.table.return_value.insert)
    app.dependency_overrides.clear()


def _violation(index):
    return APIError(
        {
            "code": "23505",
            "message": f'duplicate key value violates unique constraint "{index}"',
            "details": "Key (repository_id, target_issue_number)=(1, 12) already exists.",
        }
    )


@pytest.mark.parametrize(
    "github_id, login", [(4242, "octo"), (None, None)], ids=["linked", "no-github"]
)
def test_start_records_its_trigger_in_the_run(start, github_id, login):
    caller = {**USER, "github_id": github_id, "github_login": login}

    async def user():
        return caller

    app.dependency_overrides[main.get_current_user] = user

    response = client.post("/start", json={"repo_name": "o/r", "target_issue": 12})

    assert response.status_code == 200, response.text
    [row] = start.insert.call_args.args
    assert row["trigger_source"] == "api"
    assert row["trigger_github_id"] == github_id
    assert row["trigger_login"] == login
    assert row["user_id"] == "u1"
    # The same trigger goes to the worker's trust gate.
    kwargs = start.task.apply_async.call_args.kwargs["kwargs"]
    assert kwargs["trigger_source"] == "api"
    assert kwargs["trigger_github_id"] == github_id
    assert kwargs["trigger_login"] == login


def test_start_for_an_issue_with_an_active_run_is_409(start):
    start.insert.return_value.execute.side_effect = _violation(
        "runs_one_active_per_issue"
    )

    response = client.post("/start", json={"repo_name": "o/r", "target_issue": 12})

    assert response.status_code == 409
    body = response.json()
    assert body["code"] == main.RUN_ALREADY_ACTIVE == "RUN_ALREADY_ACTIVE"
    assert "#12" in body["detail"]
    assert "duplicate key" not in response.text
    start.task.apply_async.assert_not_called()


def test_other_insert_failures_stay_500(start):
    start.insert.return_value.execute.side_effect = _violation("runs_pkey")

    response = client.post("/start", json={"repo_name": "o/r", "target_issue": 12})

    assert response.status_code == 500
    assert "code" not in response.json()
    start.task.apply_async.assert_not_called()


def test_start_without_a_target_issue_is_not_checked_by_the_backend(start):
    """The index covers only runs with a target issue; /start adds no check
    of its own, so a run without one is inserted as before."""
    response = client.post("/start", json={"repo_name": "o/r"})

    assert response.status_code == 200, response.text
    [row] = start.insert.call_args.args
    assert row["target_issue_number"] is None
    start.task.apply_async.assert_called_once()
