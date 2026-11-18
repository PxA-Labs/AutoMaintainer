"""Processing of stored webhook_events rows (#391)."""

import hashlib
import hmac
import json
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

import celery_app as celery_module
import github_app
import webhook_processor
from webhook_processor import process_event, process_pending_events


class Query:
    def __init__(self, db, table):
        self.db = db
        self.table = table
        self.filters = []
        self.patch = None
        self.one = False
        self.inserted = None  # the row an insert() will write
        self.max_rows = None

    def select(self, *_a):
        return self

    def update(self, fields):
        self.patch = fields
        return self

    def eq(self, col, val):
        self.filters.append(lambda r: r.get(col) == val)
        return self

    def in_(self, col, values):
        self.filters.append(lambda r: r.get(col) in values)
        return self

    def is_(self, col, val):
        assert val == "null"
        self.filters.append(lambda r: r.get(col) is None)
        return self

    def order(self, *_a, **_k):
        return self

    def limit(self, n):
        self.max_rows = n
        return self

    def maybe_single(self):
        self.one = True
        return self

    # github_app's installation lookup still uses .single().
    single = maybe_single

    def insert(self, row):
        # Written at execute(), as postgrest-py does: that is where a
        # constraint violation is raised.
        self.inserted = row
        return self

    def execute(self):
        # One statement at a time, as the database runs each atomically:
        # the unique-index check and the write cannot interleave (threads).
        with self.db.lock:
            return self._execute()

    def _execute(self):
        table = self.db[self.table]
        if self.inserted is not None:
            # Column defaults from supabase_schema.sql.
            defaults = {"retry_count": 0, "max_retries": 3, "processed_at": None}
            new = {**defaults, "id": f"row-{len(table)}", **self.inserted}
            self.db.check_unique(self.table, [new], table)
            table.append(new)
            return SimpleNamespace(data=[])
        rows = [r for r in table if all(f(r) for f in self.filters)]
        if self.patch is not None:
            self.db.calls.append((self.table, dict(self.patch)))
            updated = [{**row, **self.patch} for row in rows]
            others = [r for r in table if not any(r is row for row in rows)]
            self.db.check_unique(self.table, updated, others)
            for row in rows:
                row.update(self.patch)
            return SimpleNamespace(data=[dict(r) for r in rows])
        if self.max_rows is not None:
            rows = rows[: self.max_rows]
        if self.one:
            return SimpleNamespace(data=dict(rows[0])) if rows else None
        return SimpleNamespace(data=[dict(r) for r in rows])


def _active_issue_key(row):
    """runs_one_active_per_issue's key for ``row``, or None outside the index
    (supabase/migrations/20261117140000_run_trigger_and_active_issue.sql:
    (repository_id, target_issue_number) where the status is active and the
    target issue is not null)."""
    if row.get("target_issue_number") is None:
        return None
    if row.get("status") not in webhook_processor.ACTIVE_RUN_STATUSES:
        return None
    return (row.get("repository_id"), row["target_issue_number"])


def _unique_violation(index, key):
    """What postgrest-py raises when Postgres reports SQLSTATE 23505."""
    from postgrest.exceptions import APIError

    return APIError(
        {
            "code": "23505",
            "message": f'duplicate key value violates unique constraint "{index}"',
            "details": f"Key (repository_id, target_issue_number)={key} "
            "already exists.",
        }
    )


class FakeDB(dict):
    """In-memory tables. Enforces the one unique index the code relies on
    for correctness, runs_one_active_per_issue (#518, #524); nothing else."""

    def __init__(self, **tables):
        super().__init__(tables)
        self.calls = []
        self.lock = threading.RLock()

    def check_unique(self, table, written, others):
        """Raise the index's 23505 if ``written`` (the rows a statement
        writes, as they will be) clash with each other or with ``others``
        (the rows it leaves alone)."""
        if table != "runs":
            return
        seen = {_active_issue_key(r) for r in others} - {None}
        for row in written:
            key = _active_issue_key(row)
            if key is None:
                continue
            if key in seen:
                raise _unique_violation(webhook_processor.ACTIVE_RUN_INDEX, key)
            seen.add(key)

    def table(self, name):
        self.setdefault(name, [])
        return Query(self, name)


def event(event_type, action, **extra):
    row = {
        "id": f"ev-{event_type}-{action}",
        "org_id": "org-1",
        "github_installation_id": 7,
        "github_delivery_id": f"d-{event_type}-{action}",
        "event_type": event_type,
        "action": action,
        "payload": {},
        "status": "pending",
        "retry_count": 0,
        "max_retries": 3,
        "processed_at": None,
    }
    row.update(extra)
    return row


def installation(**extra):
    return {"id": 7, "org_id": "org-1", "suspended_at": None, **extra}


@pytest.fixture
def sync(monkeypatch):
    mock = AsyncMock(return_value=3)
    monkeypatch.setattr(webhook_processor, "sync_installation_repositories", mock)
    return mock


@pytest.mark.parametrize("action", ["deleted", "suspend"])
def test_installation_deleted_or_suspended_is_deactivated(sync, action):
    ev = event("installation", action)
    db = FakeDB(webhook_events=[ev], github_installations=[installation()])

    assert process_event(db, dict(ev)) == "completed"

    assert db["github_installations"][0]["suspended_at"] is not None
    stored = db["webhook_events"][0]
    assert stored["status"] == "completed"
    assert stored["processed_at"] is not None
    sync.assert_not_called()


@pytest.mark.parametrize("action", ["deleted", "suspend"])
def test_deactivation_drops_cached_installation_tokens(sync, action):
    github_app._installation_token_cache.clear()
    github_app._installation_token_cache[(7, "o/r", None)] = ("tok", 9e12)
    github_app._installation_token_cache[(8, None, None)] = ("other", 9e12)
    ev = event("installation", action)
    db = FakeDB(webhook_events=[ev], github_installations=[installation()])

    try:
        assert process_event(db, dict(ev)) == "completed"
        assert list(github_app._installation_token_cache) == [(8, None, None)]
    finally:
        github_app._installation_token_cache.clear()


def test_suspend_records_githubs_suspension_time(sync):
    ev = event(
        "installation",
        "suspend",
        payload={"installation": {"id": 7, "suspended_at": "2026-11-01T10:00:00Z"}},
    )
    db = FakeDB(webhook_events=[ev], github_installations=[installation()])

    assert process_event(db, dict(ev)) == "completed"

    assert db["github_installations"][0]["suspended_at"] == "2026-11-01T10:00:00Z"


def test_installation_unsuspend_reactivates_and_resyncs(sync):
    ev = event("installation", "unsuspend")
    db = FakeDB(
        webhook_events=[ev],
        github_installations=[installation(suspended_at="2026-01-01")],
    )

    assert process_event(db, dict(ev)) == "completed"

    assert db["github_installations"][0]["suspended_at"] is None
    sync.assert_awaited_once_with(db, 7, "org-1")


def test_installation_update_of_other_org_is_not_touched(sync):
    ev = event("installation", "deleted")
    other = installation(org_id="org-2")
    db = FakeDB(webhook_events=[ev], github_installations=[other])

    process_event(db, dict(ev))

    assert other["suspended_at"] is None


@pytest.mark.parametrize("action", ["added", "removed"])
def test_installation_repositories_change_resyncs(sync, action):
    ev = event("installation_repositories", action)
    db = FakeDB(webhook_events=[ev], github_installations=[installation()])

    assert process_event(db, dict(ev)) == "completed"

    sync.assert_awaited_once_with(db, 7, "org-1")


def test_repositories_change_for_suspended_installation_is_ignored(sync):
    ev = event("installation_repositories", "added")
    db = FakeDB(
        webhook_events=[ev],
        github_installations=[installation(suspended_at="2026-01-01")],
    )

    assert process_event(db, dict(ev)) == "ignored"
    sync.assert_not_called()


@pytest.mark.parametrize(
    "event_type,action",
    [
        ("pull_request", "opened"),
        ("issues", "closed"),
        ("installation", "created"),
        # #190 lists these; no product behaviour depends on them yet.
        ("issues", "opened"),
        ("pull_request", "closed"),
    ],
)
def test_other_events_are_explicitly_ignored(sync, event_type, action):
    ev = event(event_type, action)
    db = FakeDB(webhook_events=[ev], github_installations=[installation()])

    assert process_event(db, dict(ev)) == "ignored"

    stored = db["webhook_events"][0]
    assert stored["status"] == "ignored"
    assert stored["processed_at"] is not None
    sync.assert_not_called()


def test_already_processed_rows_are_skipped(sync):
    done = event("installation", "deleted", status="completed", processed_at="t")
    db = FakeDB(webhook_events=[done], github_installations=[installation()])

    assert process_event(db, dict(done)) == "skipped"
    assert process_pending_events(db) == {}
    assert db.calls == []


def test_row_claimed_by_another_worker_is_skipped(sync):
    ev = event("installation_repositories", "added")
    db = FakeDB(webhook_events=[ev], github_installations=[installation()])
    stale_copy = dict(ev)
    ev["status"] = "processing"  # another worker claimed it after our read

    assert process_event(db, stale_copy) == "skipped"
    sync.assert_not_called()


def test_processing_twice_runs_the_handler_once(sync):
    ev = event("installation_repositories", "added")
    db = FakeDB(webhook_events=[ev], github_installations=[installation()])

    assert process_pending_events(db) == {"completed": 1}
    assert process_pending_events(db) == {}
    sync.assert_awaited_once()


def test_failure_is_recorded_and_retried_until_max_retries(sync):
    sync.side_effect = RuntimeError("GitHub unavailable")
    ev = event("installation_repositories", "added", max_retries=2)
    db = FakeDB(webhook_events=[ev], github_installations=[installation()])

    assert process_pending_events(db) == {"pending": 1}
    stored = db["webhook_events"][0]
    assert stored["retry_count"] == 1
    assert "GitHub unavailable" in stored["error_message"]
    assert stored["processed_at"] is None

    assert process_pending_events(db) == {"failed": 1}
    assert stored["status"] == "failed"
    assert stored["retry_count"] == 2
    assert stored["processed_at"] is not None

    assert process_pending_events(db) == {}


def test_sweep_can_target_one_delivery(sync):
    first = event("installation", "deleted")
    second = event("pull_request", "opened")
    db = FakeDB(webhook_events=[first, second], github_installations=[installation()])

    assert process_pending_events(db, delivery_id="d-pull_request-opened") == {
        "ignored": 1
    }
    assert first["status"] == "pending"


def test_task_is_registered_on_the_github_sync_queue():
    conf = celery_module.celery_app.conf
    name = "webhook_processor.process_webhook_events"
    # The mock Celery used without the celery package ignores conf.update.
    if conf:
        assert conf["task_routes"][name] == {"queue": "github_sync"}
        assert any(e["task"] == name for e in conf["beat_schedule"].values())
    assert callable(webhook_processor.process_webhook_events_task)


# --- Route wiring --------------------------------------------------------------

SECRET = "test-webhook-secret"


def _post(monkeypatch, body: bytes, delivery="delivery-1"):
    import main

    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", SECRET)
    monkeypatch.setattr(github_app, "GITHUB_WEBHOOK_SECRET", SECRET)
    sig = "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    return TestClient(main.app).post(
        "/webhooks/github",
        content=body,
        headers={
            "X-GitHub-Event": "installation_repositories",
            "X-GitHub-Delivery": delivery,
            "X-Hub-Signature-256": sig,
        },
    )


def _webhook_db(existing=False):
    db = FakeDB(github_installations=[installation()])
    if existing:
        db["webhook_events"] = [
            event("installation_repositories", "added", github_delivery_id="dup")
        ]
    return db


def test_route_enqueues_processing_when_broker_configured(monkeypatch):
    import agents
    import main

    db = _webhook_db()
    monkeypatch.setattr(agents, "supabase", db)
    monkeypatch.setattr(main, "redis_configured", lambda: True)
    task = MagicMock()
    monkeypatch.setattr(main, "process_webhook_events_task", task)

    body = json.dumps({"action": "added", "installation": {"id": 7}}).encode()
    response = _post(monkeypatch, body)

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "queued"
    task.apply_async.assert_called_once_with(
        kwargs={"delivery_id": "delivery-1"}, queue="github_sync"
    )


def test_route_still_accepts_delivery_when_enqueue_fails(monkeypatch):
    import agents
    import main

    monkeypatch.setattr(agents, "supabase", _webhook_db())
    monkeypatch.setattr(main, "redis_configured", lambda: True)
    task = MagicMock()
    task.apply_async.side_effect = ConnectionError("broker down")
    monkeypatch.setattr(main, "process_webhook_events_task", task)

    body = json.dumps({"action": "added", "installation": {"id": 7}}).encode()
    response = _post(monkeypatch, body)

    assert response.status_code == 200
    assert response.json()["status"] == "queued"


def test_route_processes_inline_without_broker(monkeypatch, sync):
    import agents
    import main

    db = _webhook_db()
    monkeypatch.setattr(agents, "supabase", db)
    monkeypatch.setattr(main, "redis_configured", lambda: False)

    body = json.dumps({"action": "added", "installation": {"id": 7}}).encode()
    response = _post(monkeypatch, body)

    assert response.status_code == 200
    stored = db["webhook_events"][0]
    assert stored["status"] == "completed"
    sync.assert_awaited_once_with(db, 7, "org-1")


def test_route_does_not_enqueue_duplicates(monkeypatch):
    import agents
    import main

    monkeypatch.setattr(agents, "supabase", _webhook_db(existing=True))
    monkeypatch.setattr(main, "redis_configured", lambda: True)
    task = MagicMock()
    monkeypatch.setattr(main, "process_webhook_events_task", task)

    body = json.dumps({"action": "added", "installation": {"id": 7}}).encode()
    response = _post(monkeypatch, body, delivery="dup")

    assert response.json()["status"] == "duplicate"
    task.apply_async.assert_not_called()


# --- The fake enforces runs_one_active_per_issue (#518, #524) ------------------


def _run(run_id, status="queued", issue=12, repository_id=55):
    return {
        "id": run_id,
        "repository_id": repository_id,
        "target_issue_number": issue,
        "status": status,
    }


def _refused(write):
    with pytest.raises(Exception) as caught:
        write()
    assert webhook_processor.is_active_run_conflict(caught.value), caught.value


@pytest.mark.parametrize("status", webhook_processor.ACTIVE_RUN_STATUSES)
def test_fake_db_refuses_a_second_active_run_for_an_issue(status):
    db = FakeDB(runs=[_run("r1", status=status)])

    _refused(lambda: db.table("runs").insert(_run("r2")).execute())

    assert [r["id"] for r in db["runs"]] == ["r1"]


@pytest.mark.parametrize(
    "existing, new",
    [
        (_run("r1", status="completed"), _run("r2")),  # the old run is over
        (_run("r1", status="failed"), _run("r2")),
        (_run("r1"), _run("r2", issue=13)),  # another issue
        (_run("r1"), _run("r2", repository_id=56)),  # another repository
        (_run("r1", issue=None), _run("r2", issue=None)),  # no target issue
        (_run("r1"), _run("r2", status="cancelled")),  # the new row is inactive
    ],
)
def test_fake_db_allows_what_the_index_allows(existing, new):
    db = FakeDB(runs=[existing])

    db.table("runs").insert(new).execute()

    assert [r["id"] for r in db["runs"]] == ["r1", "r2"]


def test_fake_db_refuses_an_update_that_reactivates_a_second_run():
    db = FakeDB(runs=[_run("r1"), _run("r2", status="failed")])

    _refused(
        lambda: db.table("runs").update({"status": "queued"}).eq("id", "r2").execute()
    )

    assert db["runs"][1]["status"] == "failed"
    # Updating the active run itself (queued -> running) is fine.
    db.table("runs").update({"status": "running"}).eq("id", "r1").execute()
    assert db["runs"][0]["status"] == "running"
