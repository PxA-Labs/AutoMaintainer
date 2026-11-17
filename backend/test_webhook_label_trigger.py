"""#190: a trigger label on an issue starts an agent run, and webhook
signatures are rejected with 401.

GitHub, Supabase and Celery are faked: the database is test_webhook_processor's
in-memory FakeDB, the Celery task and the worker check are mocks.
"""

import hashlib
import hmac
import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

import github_app
import tasks
import trust
import webhook_processor
import worker_probe
from test_webhook_processor import FakeDB, event, installation
from webhook_processor import process_event, process_pending_events

LABEL = "automaintainer"
OWNER_ID = "owner-user-uuid"
MAINTAINER = {"id": 1001, "login": "maintainer", "type": "User"}
OUTSIDER = {"id": 2002, "login": "outsider", "type": "User"}


def labeled(label=LABEL, sender=MAINTAINER, issue=None, repo_id=55, **extra):
    """A stored issues.labeled row as handle_github_webhook writes it."""
    payload = {
        "action": "labeled",
        "label": {"name": label},
        "issue": {
            "number": 12,
            "state": "open",
            "user": OUTSIDER,
            "author_association": "NONE",
            **(issue or {}),
        },
        "repository": {"id": repo_id, "full_name": "acme/widgets"},
        "sender": sender,
        "installation": {"id": 7},
    }
    return event("issues", "labeled", payload=payload, **extra)


def world(*events, runs=None, repositories=None, owners=True):
    return FakeDB(
        webhook_events=list(events),
        github_installations=[installation()],
        repositories=(
            repositories
            if repositories is not None
            else [
                {
                    "id": 55,
                    "org_id": "org-1",
                    "github_installation_id": 7,
                    "full_name": "acme/widgets",
                }
            ]
        ),
        organization_members=(
            [
                {"org_id": "org-1", "user_id": "member-uuid", "role": "member"},
                {"org_id": "org-1", "user_id": OWNER_ID, "role": "owner"},
            ]
            if owners
            else []
        ),
        runs=list(runs or []),
    )


@pytest.fixture
def trigger(monkeypatch):
    """Feature on, a worker available, Celery and run logging mocked."""
    monkeypatch.setenv(webhook_processor.TRIGGER_LABEL_ENV, LABEL)
    worker = MagicMock(return_value=True)
    monkeypatch.setattr(webhook_processor, "_agent_worker_available", worker)
    task = MagicMock()
    task.apply_async.return_value = SimpleNamespace(id="celery-1")
    monkeypatch.setattr(tasks, "run_agent_loop_task", task)
    monkeypatch.setattr(tasks, "log_run_event", AsyncMock())
    return SimpleNamespace(task=task, worker=worker)


def _process(db, ev):
    return process_event(db, dict(ev))


# --- A run is started like POST /start ------------------------------------------


def test_trigger_label_queues_one_run_like_start(trigger):
    ev = labeled()
    db = world(ev)

    assert _process(db, ev) == "completed"

    [run] = db["runs"]
    assert run["org_id"] == "org-1"
    assert run["user_id"] == OWNER_ID
    assert run["repository_id"] == 55
    assert run["github_installation_id"] == 7
    assert run["repo_name"] == "acme/widgets"
    assert run["target_issue_number"] == 12
    assert run["mode"] == "autonomous"
    assert run["status"] == "queued"
    assert run["celery_task_id"] == "celery-1"
    trigger.task.apply_async.assert_called_once_with(
        args=[run["id"], "acme/widgets", "org-1", OWNER_ID, 55, 7],
        kwargs={
            "target_issue_number": 12,
            "mode": "autonomous",
            "trigger_login": "maintainer",
            "trigger_github_id": 1001,
            "trigger_source": "label",
        },
        queue="agent_runs",
    )
    tasks.log_run_event.assert_awaited_once()
    assert "id 1001" in tasks.log_run_event.await_args.kwargs["message"]


def test_label_name_is_compared_case_insensitively(trigger):
    ev = labeled(label="AutoMaintainer")
    db = world(ev)

    assert _process(db, ev) == "completed"
    assert len(db["runs"]) == 1


@pytest.mark.parametrize("label", ["bug", "automaintainer-later", None])
def test_other_labels_start_nothing(trigger, label):
    ev = labeled(label=label)
    db = world(ev)

    assert _process(db, ev) == "ignored"
    assert db["runs"] == []
    trigger.task.apply_async.assert_not_called()


@pytest.mark.parametrize("value", [None, "", "   "])
def test_feature_is_off_without_a_trigger_label(trigger, monkeypatch, value):
    if value is None:
        monkeypatch.delenv(webhook_processor.TRIGGER_LABEL_ENV)
    else:
        monkeypatch.setenv(webhook_processor.TRIGGER_LABEL_ENV, value)
    ev = labeled()
    db = world(ev)

    assert _process(db, ev) == "ignored"
    assert db["runs"] == []
    trigger.worker.assert_not_called()


@pytest.mark.parametrize(
    "issue",
    [{"state": "closed"}, {"pull_request": {"url": "u"}}, {"number": None}],
    ids=["closed", "pull-request", "no-number"],
)
def test_only_open_issues_start_a_run(trigger, issue):
    ev = labeled(issue=issue)
    db = world(ev)

    assert _process(db, ev) == "ignored"
    assert db["runs"] == []


# --- Who may start a run --------------------------------------------------------


class _Repo:
    """GitHub as trust.check_trigger sees it: the issue and permissions."""

    def __init__(self, permissions, issue_author):
        self.permissions = permissions
        self.issue_author = issue_author

    def get_collaborator_permission(self, login):
        return self.permissions.get(login, "none")

    def get_issue(self, number):
        return SimpleNamespace(user=SimpleNamespace(**self.issue_author))


class _Users:
    def __init__(self, *people):
        self.by_id = {p["id"]: p for p in people}

    def get_user_by_id(self, github_id):
        return SimpleNamespace(**self.by_id[github_id])


def _gate(trigger_kwargs, repo, users):
    """The merge decision the worker makes for the queued run."""
    return trust.check_trigger(
        repo,
        {
            "source": trigger_kwargs["trigger_source"],
            "login": trigger_kwargs["trigger_login"],
            "github_id": trigger_kwargs["trigger_github_id"],
        },
        trigger_kwargs["target_issue_number"],
        users,
    )


def test_untrusted_adder_gets_a_run_that_the_gate_never_merges(trigger):
    """A triager (no write access) can label: the run opens a PR for a human."""
    ev = labeled(issue={"user": MAINTAINER, "author_association": "MEMBER"})
    db = world(ev)

    assert _process(db, ev) == "completed"
    kwargs = trigger.task.apply_async.call_args.kwargs["kwargs"]
    assert kwargs["trigger_github_id"] == MAINTAINER["id"]

    repo = _Repo({"maintainer": "triage"}, MAINTAINER)
    decision = _gate(kwargs, repo, _Users(MAINTAINER))
    assert decision.trusted is False

    writer_repo = _Repo({"maintainer": "write"}, MAINTAINER)
    assert _gate(kwargs, writer_repo, _Users(MAINTAINER)).trusted is True


def test_writer_label_on_an_outsiders_issue_is_still_not_auto_merged(trigger):
    ev = labeled()  # issue opened by OUTSIDER, label added by MAINTAINER
    db = world(ev)

    assert _process(db, ev) == "completed"
    kwargs = trigger.task.apply_async.call_args.kwargs["kwargs"]
    repo = _Repo({"maintainer": "write", "outsider": "read"}, OUTSIDER)
    assert _gate(kwargs, repo, _Users(MAINTAINER, OUTSIDER)).trusted is False


def test_bot_adder_gets_no_run(trigger):
    bot = {"id": 41898282, "login": "github-actions[bot]", "type": "Bot"}
    ev = labeled(sender=bot)
    db = world(ev)

    assert _process(db, ev) == "ignored"
    assert db["runs"] == []


def test_adder_without_an_id_gets_no_run(trigger):
    ev = labeled(sender={"login": "ghost", "type": "User"})
    db = world(ev)

    assert _process(db, ev) == "ignored"
    assert db["runs"] == []


def test_outsider_labelling_own_issue_via_template_gets_no_run(trigger):
    """GitHub applies an issue template's labels as the issue's author."""
    ev = labeled(sender=OUTSIDER)
    db = world(ev)

    assert _process(db, ev) == "ignored"
    assert db["runs"] == []
    trigger.task.apply_async.assert_not_called()


@pytest.mark.parametrize("association", ["OWNER", "MEMBER", "COLLABORATOR"])
def test_insider_labelling_own_issue_gets_a_run(trigger, association):
    ev = labeled(
        sender=MAINTAINER,
        issue={"user": MAINTAINER, "author_association": association},
    )
    db = world(ev)

    assert _process(db, ev) == "completed"
    assert len(db["runs"]) == 1


# --- Guards ----------------------------------------------------------------------


@pytest.mark.parametrize("status", ["queued", "running", "awaiting_review"])
def test_no_second_run_while_one_is_active(trigger, status):
    existing = {
        "id": "run-0",
        "repository_id": 55,
        "target_issue_number": 12,
        "status": status,
    }
    ev = labeled()
    db = world(ev, runs=[existing])

    assert _process(db, ev) == "ignored"
    assert db["runs"] == [existing]
    trigger.task.apply_async.assert_not_called()


@pytest.mark.parametrize("status", ["completed", "failed", "cancelled"])
def test_finished_run_does_not_block_a_new_one(trigger, status):
    finished = {
        "id": "run-0",
        "repository_id": 55,
        "target_issue_number": 12,
        "status": status,
    }
    other_issue = {
        "id": "run-1",
        "repository_id": 55,
        "target_issue_number": 99,
        "status": "running",
    }
    ev = labeled()
    db = world(ev, runs=[finished, other_issue])

    assert _process(db, ev) == "completed"
    assert len(db["runs"]) == 3


@pytest.mark.parametrize(
    "repositories",
    [
        [],
        [
            {
                "id": 55,
                "org_id": "org-1",
                "github_installation_id": 8,
                "full_name": "acme/widgets",
            }
        ],
        [
            {
                "id": 55,
                "org_id": "org-2",
                "github_installation_id": 7,
                "full_name": "acme/widgets",
            }
        ],
    ],
    ids=["unknown", "other-installation", "other-org"],
)
def test_unregistered_repository_is_ignored_with_a_log_line(
    trigger, caplog, repositories
):
    ev = labeled()
    db = world(ev, repositories=repositories)

    with caplog.at_level(logging.INFO, logger="webhook_processor"):
        assert _process(db, ev) == "ignored"

    assert db["runs"] == []
    assert "not registered" in caplog.text


def test_suspended_installation_is_ignored(trigger):
    ev = labeled()
    db = world(ev)
    db["github_installations"][0]["suspended_at"] = "2026-01-01"

    assert _process(db, ev) == "ignored"
    assert db["runs"] == []


def test_no_worker_means_no_run(trigger, caplog):
    trigger.worker.return_value = False
    ev = labeled()
    db = world(ev)

    with caplog.at_level(logging.WARNING, logger="webhook_processor"):
        assert _process(db, ev) == "ignored"

    assert db["runs"] == []
    trigger.task.apply_async.assert_not_called()
    assert "no agent worker" in caplog.text
    assert db["webhook_events"][0]["status"] == "ignored"


def test_org_without_owner_gets_no_run(trigger):
    ev = labeled()
    db = world(ev, owners=False)

    assert _process(db, ev) == "ignored"
    assert db["runs"] == []


def test_enqueue_failure_fails_the_run_and_retries_the_event(trigger):
    trigger.task.apply_async.side_effect = ConnectionError("broker down")
    ev = labeled()
    db = world(ev)

    assert _process(db, ev) == "pending"

    [run] = db["runs"]
    assert run["status"] == "failed"
    assert "broker down" in run["error_message"]
    assert db["webhook_events"][0]["retry_count"] == 1


def test_processing_the_event_twice_starts_one_run(trigger):
    ev = labeled()
    db = world(ev)

    assert process_pending_events(db) == {"completed": 1}
    assert process_pending_events(db) == {}
    assert len(db["runs"]) == 1


# --- The worker check is POST /start's ---------------------------------------------


def test_worker_check_uses_starts_check(monkeypatch):
    check = AsyncMock(return_value=False)
    monkeypatch.setattr(worker_probe, "agent_worker_available", check)
    assert webhook_processor._agent_worker_available() is False
    check.return_value = True
    assert webhook_processor._agent_worker_available() is True
    assert check.await_count == 2


def test_worker_check_fails_closed(monkeypatch):
    monkeypatch.setattr(
        worker_probe,
        "agent_worker_available",
        AsyncMock(side_effect=RuntimeError("x")),
    )
    assert webhook_processor._agent_worker_available() is False


# --- Through the HTTP route --------------------------------------------------------

SECRET = "test-webhook-secret"


def _post(monkeypatch, body: bytes, delivery="delivery-1", signature=None, **hdrs):
    import main

    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", SECRET)
    monkeypatch.setattr(github_app, "GITHUB_WEBHOOK_SECRET", SECRET)
    if signature is None:
        digest = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
        signature = f"sha256={digest}"
    headers = {"X-GitHub-Event": "issues", "X-GitHub-Delivery": delivery}
    if signature is not False:
        headers["X-Hub-Signature-256"] = signature
    headers.update(hdrs)
    return TestClient(main.app).post("/webhooks/github", content=body, headers=headers)


def test_redelivered_label_starts_one_run(monkeypatch, trigger):
    import agents
    import main

    db = world()
    monkeypatch.setattr(agents, "supabase", db)
    monkeypatch.setattr(main, "redis_configured", lambda: False)
    body = json.dumps(labeled()["payload"]).encode()

    first = _post(monkeypatch, body, delivery="same")
    again = _post(monkeypatch, body, delivery="same")

    assert first.json()["status"] == "queued"
    assert again.json()["status"] == "duplicate"
    assert len(db["webhook_events"]) == 1
    assert db["webhook_events"][0]["status"] == "completed"
    assert len(db["runs"]) == 1
    trigger.task.apply_async.assert_called_once()
