"""#487: an automatic merge (MERGE_POLICY=ci or auto) needs a trusted trigger.

The run's trigger (the API caller's linked GitHub login, plus the author of a
targeted issue) must have write, maintain or admin permission on the
repository, looked up deterministically through the run's GitHub client.
Anything else, including a failed lookup, holds the PR for a human.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from github import GithubException

import agents
import main
import merge_audit
import tasks
import trust
from events import NullEmitter
from test_agent_idempotency import RUN_ID, WRITER, WorkerCrash, world  # noqa: F401
from test_agent_idempotency import _run
from test_agent_merge_policy import VERIFIED, _setup, _state

# --- trust.check_trigger -----------------------------------------------------------


class PermRepo:
    """The collaborator-permission and issue endpoints of a repository."""

    def __init__(self, permissions=None, issue_user=None, issue_error=None):
        self.permissions = permissions or {}
        self.lookups = []
        self.issue_user = issue_user
        self.issue_error = issue_error

    def get_collaborator_permission(self, login):
        self.lookups.append(login)
        found = self.permissions.get(login, "none")
        if isinstance(found, Exception):
            raise found
        return found

    def get_issue(self, number):
        if self.issue_error:
            raise self.issue_error
        return SimpleNamespace(number=number, user=self.issue_user)


def _api(login):
    return {"source": "api", "login": login}


@pytest.mark.parametrize("permission", ["admin", "write"])
def test_writer_trigger_is_trusted(permission):
    repo = PermRepo({"ann": permission})

    found = trust.check_trigger(repo, _api("ann"), None)

    assert found.trusted is True
    assert found.reason == merge_audit.REASON_TRUSTED_TRIGGER
    assert repo.lookups == ["ann"]


@pytest.mark.parametrize("permission", ["read", "none", "triage", "", None])
def test_reader_or_outsider_trigger_is_untrusted(permission):
    found = trust.check_trigger(PermRepo({"eve": permission}), _api("eve"), None)

    assert found.trusted is False
    assert found.reason == merge_audit.REASON_UNTRUSTED_TRIGGER
    assert "eve" in found.detail


@pytest.mark.parametrize(
    "trigger",
    [
        None,
        {},
        _api(None),
        _api(""),
        _api("dependabot[bot]"),
        _api("../admin"),
        _api("a" * 40),
        _api(["ann"]),
    ],
)
def test_unknown_bot_or_malformed_trigger_is_untrusted_without_a_lookup(trigger):
    repo = PermRepo({"ann": "admin", "dependabot[bot]": "admin"})

    found = trust.check_trigger(repo, trigger, None)

    assert found.trusted is False
    assert found.reason == merge_audit.REASON_UNTRUSTED_TRIGGER
    assert repo.lookups == []


def test_non_collaborator_404_is_untrusted():
    repo = PermRepo({"eve": GithubException(404, {"message": "Not Found"}, None)})

    found = trust.check_trigger(repo, _api("eve"), None)

    assert (found.trusted, found.reason) == (False, "untrusted trigger")


@pytest.mark.parametrize(
    "error",
    [
        GithubException(403, {"message": "Resource not accessible"}, None),
        GithubException(500, {"message": "boom"}, None),
        ConnectionError("network down"),
    ],
)
def test_failed_permission_lookup_fails_closed(error):
    found = trust.check_trigger(PermRepo({"ann": error}), _api("ann"), None)

    assert found.trusted is False
    assert found.reason == merge_audit.REASON_TRIGGER_CHECK_FAILED


def test_targeted_issue_by_an_outsider_is_untrusted_even_for_a_writer_caller():
    """The issue body is the task the models follow."""
    repo = PermRepo(
        {"ann": "write", "eve": "read"},
        issue_user=SimpleNamespace(login="eve", type="User"),
    )

    found = trust.check_trigger(repo, _api("ann"), 12)

    assert found.trusted is False
    assert found.reason == merge_audit.REASON_UNTRUSTED_TRIGGER
    assert "author of issue #12 eve has read permission" in found.detail


def test_targeted_issue_by_a_writer_is_trusted_with_one_lookup_per_login():
    repo = PermRepo(
        {"ann": "write"}, issue_user=SimpleNamespace(login="ann", type="User")
    )

    found = trust.check_trigger(repo, _api("ann"), 12)

    assert found.trusted is True
    assert repo.lookups == ["ann"]


def test_targeted_issue_by_a_bot_is_untrusted():
    repo = PermRepo(
        {"ann": "write", "evil[bot]": "admin"},
        issue_user=SimpleNamespace(login="evil[bot]", type="Bot"),
    )

    found = trust.check_trigger(repo, _api("ann"), 12)

    assert (found.trusted, found.reason) == (False, "untrusted trigger")


def test_unreadable_issue_author_fails_closed():
    repo = PermRepo(
        {"ann": "write"}, issue_error=GithubException(502, {"message": "x"}, None)
    )

    found = trust.check_trigger(repo, _api("ann"), 12)

    assert (found.trusted, found.reason) == (False, "trigger permission check failed")


# --- The gate in maintainer_node -----------------------------------------------------


@pytest.fixture
def recorded(monkeypatch):
    record = MagicMock(return_value=True)
    monkeypatch.setattr(merge_audit, "record_decision", record)
    token = agents.current_run_id.set(RUN_ID)
    yield record
    agents.current_run_id.reset(token)


def _green(monkeypatch, policy):
    monkeypatch.setenv("MERGE_POLICY", policy)
    return _setup(
        monkeypatch, statuses=[VERIFIED], check_runs=[("tests", "completed", "success")]
    )


@pytest.mark.parametrize("policy", ["ci", "auto"])
@pytest.mark.parametrize(
    "login, permission, reason",
    [
        ("eve", "read", "untrusted trigger"),
        (None, None, "untrusted trigger"),
        ("ann", GithubException(500, {}, None), "trigger permission check failed"),
        ("ann", RuntimeError("timeout"), "trigger permission check failed"),
    ],
    ids=["reader", "unknown", "http-error", "client-error"],
)
async def test_untrusted_or_unverifiable_trigger_holds_the_pr(
    monkeypatch, recorded, policy, login, permission, reason
):
    repo, pr, _ = _green(monkeypatch, policy)
    repo.get_collaborator_permission.side_effect = lambda _login: (
        (_ for _ in ()).throw(permission)
        if isinstance(permission, Exception)
        else permission
    )

    result = await agents.maintainer_node(
        {**_state(), "trigger": {"source": "api", "login": login}}
    )

    pr.merge.assert_not_called()
    assert result["merge_status"] == "awaiting_human_review"
    pr.create_review_request.assert_called_once()
    row = recorded.call_args.kwargs
    recorded.assert_called_once()
    assert row["decision"] == "held" and row["actor"] == "policy"
    assert row["reasons"][-1] == reason
    log = " ".join(str(m) for m in result["log_messages"])
    assert f"not auto-merged: {reason}" in log


async def test_human_policy_does_not_look_up_the_trigger(monkeypatch, recorded):
    monkeypatch.setenv("MERGE_POLICY", "human")
    repo, pr, _ = _setup(monkeypatch)

    await agents.maintainer_node(_state())

    repo.get_collaborator_permission.assert_not_called()
    assert recorded.call_args.kwargs["reasons"] == [
        "maintainer approved",
        "policy human",
    ]


async def test_rejected_change_does_not_look_up_the_trigger(monkeypatch, recorded):
    monkeypatch.setenv("MERGE_POLICY", "auto")
    repo, pr, _ = _setup(
        monkeypatch, review='{"verdict": "reject", "reasons": ["Wrong."]}'
    )

    await agents.maintainer_node(_state())

    repo.get_collaborator_permission.assert_not_called()


async def test_trigger_is_looked_up_once_per_run(monkeypatch, recorded):
    repo, pr, _ = _green(monkeypatch, "auto")
    pr.merged = False
    token = agents._run_trigger_trust.set({})
    try:
        await agents.maintainer_node(_state())
        await agents.maintainer_node({**_state(), "iteration": 1})
    finally:
        agents._run_trigger_trust.reset(token)

    assert repo.get_collaborator_permission.call_count == 1
    assert pr.merge.call_count == 2  # the fake PR never reports itself merged


async def test_trusted_trigger_merges_as_before(monkeypatch, recorded):
    repo, pr, _ = _green(monkeypatch, "auto")

    result = await agents.maintainer_node(_state())

    pr.merge.assert_called_once()
    assert result["merge_status"] == "merged"
    repo.get_collaborator_permission.assert_called_once_with(WRITER_LOGIN)
    assert recorded.call_args.kwargs["reasons"][-1] == "trusted trigger"


WRITER_LOGIN = _state()["trigger"]["login"]


# --- Full runs: redelivery keeps the hold --------------------------------------------


async def test_held_run_is_not_merged_when_redelivered(
    world, monkeypatch  # noqa: F811
):
    """The original delivery held the PR for an untrusted trigger. A
    redelivery replays the checkpointed decision: no second review, no new
    lookup and no merge, even if the account has meanwhile been granted
    write access."""
    monkeypatch.setenv("MERGE_POLICY", "auto")
    world.db.runs[RUN_ID] = {"id": RUN_ID, "org_id": "org-1"}

    first = await _run(trigger_login="eve")
    world.repo.permissions["eve"] = "write"
    lookups = list(world.repo.permission_lookups)
    second = await _run(trigger_login="eve")

    assert first["merge_status"] == second["merge_status"] == "awaiting_human_review"
    assert world.repo.merges == 0
    assert world.repo.permission_lookups == lookups == ["eve"]
    assert [(d["decision"], d["reasons"][-1]) for d in world.db.decisions] == [
        ("held", "untrusted trigger")
    ]


async def test_crash_after_a_hold_holds_again_on_redelivery(
    world, monkeypatch  # noqa: F811
):
    """Crash after the held row but before the checkpoint: the redelivery
    reviews again and the gate (looked up afresh) holds again."""
    monkeypatch.setenv("MERGE_POLICY", "auto")
    world.db.runs[RUN_ID] = {"id": RUN_ID, "org_id": "org-1"}
    real = agents._request_human_review
    crashes = iter([WorkerCrash("review request")])

    def request(*args):
        error = next(crashes, None)
        if error is not None:
            raise error
        return real(*args)

    monkeypatch.setattr(agents, "_request_human_review", request)

    with pytest.raises(WorkerCrash):
        await _run(trigger_login="eve")
    result = await _run(trigger_login="eve")

    assert result["merge_status"] == "awaiting_human_review"
    assert world.repo.merges == 0
    assert [d["decision"] for d in world.db.decisions] == ["held", "held"]
    assert all(d["reasons"][-1] == "untrusted trigger" for d in world.db.decisions)


async def test_writer_triggered_run_merges_under_auto(world, monkeypatch):  # noqa: F811
    monkeypatch.setenv("MERGE_POLICY", "auto")
    world.db.runs[RUN_ID] = {"id": RUN_ID, "org_id": "org-1"}

    result = await _run()

    assert result["merge_status"] == "merged"
    assert world.repo.merges == 1
    assert world.repo.permission_lookups == [WRITER]
    assert [(d["decision"], d["reasons"][-1]) for d in world.db.decisions] == [
        ("merged", "trusted trigger")
    ]


# --- Carrying the trigger from /start to the graph -----------------------------------


def _identity(provider, **data):
    return SimpleNamespace(provider=provider, identity_data=data)


@pytest.mark.parametrize(
    "identities, login",
    [
        ([_identity("github", user_name="ann")], "ann"),
        ([_identity("github", preferred_username="ann")], "ann"),
        (
            [_identity("google", user_name="ann"), _identity("github", user_name="bo")],
            "bo",
        ),
        ([_identity("google", user_name="ann")], None),
        ([], None),
        (None, None),
    ],
)
def test_github_login_comes_from_the_github_identity(identities, login):
    user = SimpleNamespace(
        identities=identities, user_metadata={"user_name": "spoofed-admin"}
    )
    assert main.github_login_of(user) == login


def test_start_queues_the_callers_github_login_as_the_trigger(monkeypatch):
    sb = MagicMock()
    repo_q = sb.table.return_value.select.return_value.eq.return_value.eq.return_value
    repo_q.limit.return_value.maybe_single.return_value.execute.return_value = (
        SimpleNamespace(data={"id": 1, "github_installation_id": 2})
    )
    monkeypatch.setattr(agents, "supabase", sb)
    task = MagicMock()
    task.apply_async.return_value = SimpleNamespace(id="celery-1")
    monkeypatch.setattr(main, "run_agent_loop_task", task)

    async def available():
        return True

    monkeypatch.setattr(main, "agent_worker_available", available)

    async def user():
        return {
            "user_id": "u1",
            "org_id": "org-1",
            "role": "member",
            "email": "e",
            "github_login": "ann",
        }

    main.app.dependency_overrides[main.get_current_user] = user
    try:
        response = TestClient(main.app).post(
            "/start", json={"repo_name": "o/r", "target_issue": 5}
        )
    finally:
        main.app.dependency_overrides.clear()

    assert response.status_code == 200, response.text
    kwargs = task.apply_async.call_args.kwargs["kwargs"]
    assert kwargs["trigger_login"] == "ann"
    assert kwargs["trigger_source"] == "api"
    assert kwargs["target_issue_number"] == 5


def test_worker_passes_the_trigger_to_the_run(monkeypatch):
    seen = {}

    async def fake_loop(**kwargs):
        seen.update(kwargs)
        return {"status": "completed"}

    async def noop(*_a, **_k):
        return None

    monkeypatch.setattr(tasks, "run_agent_loop", fake_loop)
    monkeypatch.setattr(tasks, "_fetch_run_status", lambda _rid: "queued")
    monkeypatch.setattr(tasks, "acquire_repo_lock", lambda *_a: True)
    monkeypatch.setattr(tasks, "release_repo_lock", lambda *_a, **_k: None)
    monkeypatch.setattr(tasks, "write_heartbeat", lambda *_a, **_k: None)
    monkeypatch.setattr(tasks, "update_run_status", noop)
    monkeypatch.setattr(tasks, "record_usage_event", noop)
    monkeypatch.setattr(tasks, "log_run_event", noop)

    tasks._execute_agent_run(
        SimpleNamespace(request=SimpleNamespace(id="celery-1", retries=0)),
        run_id="run-1",
        repo_name="o/r",
        org_id="org-1",
        user_id="u",
        repository_id=1,
        github_installation_id=7,
        trigger_login="ann",
        trigger_source="api",
    )

    assert seen["trigger_login"] == "ann" and seen["trigger_source"] == "api"


def test_task_without_a_trigger_runs_with_an_unknown_one(monkeypatch):
    """A message queued before this change carries no trigger: it still
    runs, and is never merged automatically."""
    seen = {}

    def fake_task(**kwargs):
        seen.update(kwargs)

    monkeypatch.setattr(tasks, "is_redis_available", lambda: False)
    monkeypatch.setattr(tasks, "run_agent_loop_task", fake_task)
    tasks.start_agent_task(
        "run-1",
        {
            "repo_name": "o/r",
            "org_id": "org-1",
            "user_id": "u",
            "repository_id": 1,
            "github_installation_id": 7,
        },
    )
    assert seen["trigger_login"] is None and seen["trigger_source"] is None


async def test_run_agent_loop_puts_the_trigger_in_graph_state(monkeypatch):
    seen = []

    class Graph:
        async def astream(self, state, stream_mode=None):
            seen.append(state.get("trigger"))
            yield {**state, "log_messages": []}

    monkeypatch.setattr(agents, "supabase", None)
    monkeypatch.setattr(agents, "app", Graph())
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    await agents.run_agent_loop(
        "o/r",
        run_id="r1",
        emitter=NullEmitter(),
        trigger_login="ann",
        trigger_source="api",
    )
    await agents.run_agent_loop("o/r", run_id="r2", emitter=NullEmitter())

    assert seen == [
        {"source": "api", "login": "ann"},
        {"source": None, "login": None},
    ]
