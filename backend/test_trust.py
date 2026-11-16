"""#487: an automatic merge (MERGE_POLICY=ci or auto) needs a trusted trigger.

The run's trigger (the API caller's linked GitHub account, plus the author of
a targeted issue) must have write, maintain or admin permission on the
repository, looked up deterministically through the run's GitHub client.
Anything else, including a failed lookup, holds the PR for a human.

#491: every actor is identified by its numeric GitHub user id, resolved to
its current login (GET /user/{account_id}) before the permission lookup, so a
stale stored login (renamed account, freed and re-registered name) cannot
decide.
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
from test_agent_idempotency import FakeUsers, _run, user_id
from test_agent_merge_policy import VERIFIED, _setup, _state

# --- trust.check_trigger -----------------------------------------------------------


class PermRepo:
    """The collaborator-permission and issue endpoints of a repository, plus
    the run client's GET /user/{account_id} (``users``)."""

    def __init__(
        self, permissions=None, issue_user=None, issue_error=None, logins=None
    ):
        self.permissions = permissions or {}
        self.lookups = []
        self.issue_user = issue_user
        self.issue_error = issue_error
        self.users = FakeUsers(logins)

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


def _api(login, github_id=None):
    if github_id is None and isinstance(login, str) and login:
        github_id = user_id(login)
    return {"source": "api", "login": login, "github_id": github_id}


def _check(repo, trigger, target_issue=None):
    return trust.check_trigger(repo, trigger, target_issue, repo.users)


def _author(login, kind="User"):
    return SimpleNamespace(id=user_id(login), login=login, type=kind)


@pytest.mark.parametrize("permission", ["admin", "write"])
def test_writer_trigger_is_trusted(permission):
    repo = PermRepo({"ann": permission})

    found = _check(repo, _api("ann"))

    assert found.trusted is True
    assert found.reason == merge_audit.REASON_TRUSTED_TRIGGER
    assert repo.lookups == ["ann"]


@pytest.mark.parametrize("permission", ["read", "none", "triage", "", None])
def test_reader_or_outsider_trigger_is_untrusted(permission):
    found = _check(PermRepo({"eve": permission}), _api("eve"))

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

    found = _check(repo, trigger)

    assert found.trusted is False
    assert found.reason == merge_audit.REASON_UNTRUSTED_TRIGGER
    assert repo.lookups == []


def test_non_collaborator_404_is_untrusted():
    repo = PermRepo({"eve": GithubException(404, {"message": "Not Found"}, None)})

    found = _check(repo, _api("eve"))

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
    found = _check(PermRepo({"ann": error}), _api("ann"))

    assert found.trusted is False
    assert found.reason == merge_audit.REASON_TRIGGER_CHECK_FAILED


def test_targeted_issue_by_an_outsider_is_untrusted_even_for_a_writer_caller():
    """The issue body is the task the models follow."""
    repo = PermRepo(
        {"ann": "write", "eve": "read"},
        issue_user=_author("eve"),
    )

    found = _check(repo, _api("ann"), 12)

    assert found.trusted is False
    assert found.reason == merge_audit.REASON_UNTRUSTED_TRIGGER
    assert "author of issue #12 eve has read permission" in found.detail


def test_targeted_issue_by_a_writer_is_trusted_with_one_lookup_per_login():
    repo = PermRepo({"ann": "write"}, issue_user=_author("ann"))

    found = _check(repo, _api("ann"), 12)

    assert found.trusted is True
    assert repo.lookups == ["ann"]


def test_targeted_issue_by_a_bot_is_untrusted():
    repo = PermRepo(
        {"ann": "write", "evil[bot]": "admin"},
        issue_user=_author("evil[bot]", "Bot"),
    )

    found = _check(repo, _api("ann"), 12)

    assert (found.trusted, found.reason) == (False, "untrusted trigger")


def test_unreadable_issue_author_fails_closed():
    repo = PermRepo(
        {"ann": "write"}, issue_error=GithubException(502, {"message": "x"}, None)
    )

    found = _check(repo, _api("ann"), 12)

    assert (found.trusted, found.reason) == (False, "trigger permission check failed")


# --- #491: actors are checked by GitHub user id ----------------------------------


def test_renamed_account_is_checked_under_its_current_login(caplog):
    """The stored login is stale: the id now resolves to a new login, which
    has write. The permission is looked up for the new login."""
    ann = user_id("ann")
    repo = PermRepo({"ann-renamed": "write"}, logins={ann: "ann-renamed"})

    with caplog.at_level("INFO", logger="trust"):
        found = _check(repo, _api("ann", ann))

    assert found.trusted is True
    assert repo.users.lookups == [ann]
    assert repo.lookups == ["ann-renamed"]
    assert "ann-renamed" in found.detail
    assert "now ann-renamed" in caplog.text and "'ann'" in caplog.text


def test_stored_login_now_owned_by_another_account_is_not_trusted():
    """The caller signed in as "ann", then renamed; "ann" now belongs to a
    different account that has write. The caller's id resolves to its new
    login, an outsider, and "ann" is never looked up."""
    caller = user_id("mallory")
    repo = PermRepo({"ann": "write", "mallory": "read"}, logins={caller: "mallory"})

    found = _check(repo, _api("ann", caller))

    assert (found.trusted, found.reason) == (False, "untrusted trigger")
    assert repo.lookups == ["mallory"]
    assert "mallory has read permission" in found.detail


@pytest.mark.parametrize("github_id", [None, 0, -1, True, "1001", 1.5, [1001]])
def test_trigger_without_a_valid_github_id_is_untrusted_without_a_lookup(github_id):
    """A message queued before #491 (login only) or an identity without an
    id: the login of a writer is not enough."""
    repo = PermRepo({"ann": "admin"})
    trigger = {"source": "api", "login": "ann", "github_id": github_id}

    found = _check(repo, trigger)

    assert (found.trusted, found.reason) == (False, "untrusted trigger")
    assert repo.users.lookups == [] and repo.lookups == []
    assert "'ann' has no GitHub user id" in found.detail


@pytest.mark.parametrize(
    "error",
    [
        GithubException(403, {"message": "Resource not accessible"}, None),
        GithubException(502, {"message": "Bad gateway"}, None),
        ConnectionError("network down"),
    ],
)
def test_failed_user_id_lookup_fails_closed(error):
    ann = user_id("ann")
    repo = PermRepo({"ann": "write"}, logins={ann: error})

    found = _check(repo, _api("ann", ann))

    assert (found.trusted, found.reason) == (False, "trigger permission check failed")
    assert repo.lookups == []
    assert f"GitHub user {ann}" in found.detail


def test_deleted_account_is_untrusted():
    repo = PermRepo({"ghost": "write"})

    found = _check(repo, _api("ghost", 987654321))

    assert (found.trusted, found.reason) == (False, "untrusted trigger")
    assert repo.lookups == []


def test_user_id_resolving_to_a_bot_is_untrusted():
    bot = user_id("helper-bot")
    repo = PermRepo({"helper-bot": "admin"})
    repo.users.get_user_by_id = lambda i: SimpleNamespace(
        id=i, login="helper-bot", type="Bot"
    )

    found = _check(repo, _api("helper-bot", bot))

    assert (found.trusted, found.reason) == (False, "untrusted trigger")
    assert repo.lookups == []


def test_issue_author_is_checked_by_id():
    """The issue author's id decides, like the caller's: an author whose id
    now resolves to an outsider is not trusted under the login on the
    issue."""
    author = user_id("bo")
    repo = PermRepo(
        {"ann": "write", "bo": "write", "bo-renamed": "none"},
        issue_user=SimpleNamespace(id=author, login="bo", type="User"),
        logins={author: "bo-renamed"},
    )

    found = _check(repo, _api("ann"), 12)

    assert (found.trusted, found.reason) == (False, "untrusted trigger")
    assert repo.lookups == ["ann", "bo-renamed"]


def test_issue_author_without_an_id_is_untrusted():
    repo = PermRepo(
        {"ann": "write"}, issue_user=SimpleNamespace(login="ann", type="User")
    )

    found = _check(repo, _api("ann"), 12)

    assert (found.trusted, found.reason) == (False, "untrusted trigger")


def test_caller_who_wrote_the_issue_is_looked_up_once():
    repo = PermRepo({"ann": "write"}, issue_user=_author("ann"))

    found = _check(repo, _api("ann"), 12)

    assert found.trusted is True
    assert repo.users.lookups == [user_id("ann")]
    assert repo.lookups == ["ann"]


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
        ("login-only", "write", "untrusted trigger"),
        ("ann", GithubException(500, {}, None), "trigger permission check failed"),
        ("ann", RuntimeError("timeout"), "trigger permission check failed"),
    ],
    ids=["reader", "unknown", "no-github-id", "http-error", "client-error"],
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

    # "login-only": a writer's login without a GitHub user id (#491).
    trigger = (
        {"source": "api", "login": WRITER_LOGIN}
        if login == "login-only"
        else _api(login)
    )

    result = await agents.maintainer_node({**_state(), "trigger": trigger})

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
    world.db.add_run(RUN_ID, "org-1")

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
    reviews again and the gate holds again (the earlier held row is sticky)."""
    monkeypatch.setenv("MERGE_POLICY", "auto")
    world.db.add_run(RUN_ID, "org-1")
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


async def test_hold_is_sticky_when_write_access_is_granted_before_redelivery(
    world, monkeypatch  # noqa: F811
):
    """Crash after the held row, an admin then grants the trigger write, and
    the run is redelivered: the earlier held row keeps the PR held, without
    a second permission lookup."""
    monkeypatch.setenv("MERGE_POLICY", "auto")
    world.db.add_run(RUN_ID, "org-1")
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
    world.repo.permissions["eve"] = "write"
    result = await _run(trigger_login="eve")

    assert result["merge_status"] == "awaiting_human_review"
    assert world.repo.merges == 0
    assert world.repo.permission_lookups == ["eve"]
    assert [(d["decision"], d["reasons"][-1]) for d in world.db.decisions] == [
        ("held", "untrusted trigger"),
        ("held", "untrusted trigger"),
    ]


async def test_unreadable_earlier_decisions_fall_back_to_the_lookup(
    world, monkeypatch  # noqa: F811
):
    """The sticky check is best effort: when merge_decisions cannot be read,
    the fresh (fail-closed) permission lookup decides alone."""
    monkeypatch.setenv("MERGE_POLICY", "auto")
    world.db.add_run(RUN_ID, "org-1")
    real_table = world.db.table

    def table(name):
        if name == "merge_decisions":
            query = real_table(name)
            real_execute = query.execute

            def execute():
                if query.op == "select":
                    raise RuntimeError("database unavailable")
                return real_execute()

            query.execute = execute
            return query
        return real_table(name)

    monkeypatch.setattr(world.db, "table", table)

    result = await _run()

    assert result["merge_status"] == "merged"
    assert world.repo.permission_lookups == [WRITER]


async def test_writer_triggered_run_merges_under_auto(world, monkeypatch):  # noqa: F811
    monkeypatch.setenv("MERGE_POLICY", "auto")
    world.db.add_run(RUN_ID, "org-1")

    result = await _run()

    assert result["merge_status"] == "merged"
    assert world.repo.merges == 1
    assert world.repo.permission_lookups == [WRITER]
    assert [(d["decision"], d["reasons"][-1]) for d in world.db.decisions] == [
        ("merged", "trusted trigger")
    ]


# --- Carrying the trigger from /start to the graph -----------------------------------


def _identity(provider, id=None, **data):
    return SimpleNamespace(provider=provider, id=id, identity_data=data)


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


@pytest.mark.parametrize(
    "identities, github_id",
    [
        # GoTrue exposes the identity's provider_id (GitHub's user id) as id.
        ([_identity("github", id="583231", user_name="ann")], 583231),
        # ...and identity_data.sub holds the same value.
        ([_identity("github", sub="583231")], 583231),
        ([_identity("github", id="583231", sub="1")], 583231),
        (
            [_identity("google", id="77", sub="77"), _identity("github", id="9")],
            9,
        ),
        ([_identity("github", id="not-a-number", sub="12")], 12),
        ([_identity("github", id="-5")], None),
        ([_identity("github", id="0")], None),
        ([_identity("github", user_name="ann")], None),
        ([_identity("google", id="77")], None),
        ([], None),
        (None, None),
    ],
)
def test_github_id_comes_from_the_github_identity(identities, github_id):
    user = SimpleNamespace(
        identities=identities,
        user_metadata={"sub": "1", "provider_id": "1", "user_name": "spoofed"},
    )
    assert main.github_id_of(user) == github_id


def test_current_user_carries_the_github_id(monkeypatch):
    from test_authz import supabase_with_members

    member = {"user_id": "u1", "org_id": "org-1", "role": "member", "joined_at": "t"}
    sb = supabase_with_members([member])
    sb.auth.get_user.return_value.user.identities = [
        _identity("github", id="583231", user_name="ann")
    ]
    monkeypatch.setattr(agents, "supabase", sb)
    monkeypatch.delenv("AUTH_DEV_BYPASS", raising=False)

    import asyncio

    user = asyncio.run(main.get_current_user(authorization="Bearer tok"))

    assert (user["github_id"], user["github_login"]) == (583231, "ann")


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
            "github_id": 583231,
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
    assert kwargs["trigger_github_id"] == 583231
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
        trigger_github_id=583231,
    )

    assert seen["trigger_login"] == "ann" and seen["trigger_source"] == "api"
    assert seen["trigger_github_id"] == 583231


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
    assert seen["trigger_github_id"] is None


def test_worker_accepts_a_message_queued_before_the_github_id(monkeypatch):
    """A message queued by an API without #491 has no trigger_github_id
    kwarg: the task still runs it, with no id (so it is never auto-merged)."""
    seen = {}

    def execute(task, **kwargs):
        seen.update(kwargs)

    monkeypatch.setattr(tasks, "_execute_agent_run", execute)
    tasks.run_agent_loop_task(
        "run-1",
        "o/r",
        "org-1",
        "u",
        1,
        7,
        target_issue_number=None,
        mode="autonomous",
        trigger_login="ann",
        trigger_source="api",
    )

    assert seen["trigger_login"] == "ann" and seen["trigger_github_id"] is None


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
        trigger_github_id=583231,
    )
    await agents.run_agent_loop("o/r", run_id="r2", emitter=NullEmitter())

    assert seen == [
        {"source": "api", "login": "ann", "github_id": 583231},
        {"source": None, "login": None, "github_id": None},
    ]
