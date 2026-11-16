"""Epic #411: per-repository merge policy (merge_policy.py).

MERGE_POLICY is the ceiling, a repository can only tighten it, the effective
policy is read at the merge decision and fails closed to human when the
setting cannot be read. Also GET/PUT /repositories/{id}/merge-policy.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

import agents
import main
import merge_audit
import merge_policy
from merge_policy import effective
from test_agent_merge_policy import VERIFIED, _setup, _state

RUN_ID = "11111111-1111-4111-8111-111111111111"
REPO_ID = 4242
OTHER_REPO_ID = 5353


class FakeQuery:
    """The subset of the postgrest query builder these code paths use."""

    def __init__(self, client, name):
        self.client, self.name = client, name
        self.filters = []
        self.updating = None
        self.single = False

    def select(self, *a, **k):
        return self

    def limit(self, n):
        return self

    def maybe_single(self):
        self.single = True
        return self

    def eq(self, col, val):
        self.filters.append((col, val))
        return self

    def update(self, values):
        self.updating = values
        return self

    def execute(self):
        self.client.queries.append((self.name, list(self.filters), self.updating))
        if self.name in self.client.fail:
            raise RuntimeError(f"{self.name} unavailable")
        rows = [
            r
            for r in self.client.tables.get(self.name, [])
            if all(r.get(c) == v for c, v in self.filters)
        ]
        if self.updating is not None:
            for r in rows:
                r.update(self.updating)
        if self.single:  # postgrest-py's maybe_single() returns None for no row
            return SimpleNamespace(data=dict(rows[0])) if rows else None
        return SimpleNamespace(data=[dict(r) for r in rows])


class FakeClient:
    def __init__(self, setting=None, fail=(), runs=None):
        self.tables = {
            "runs": (
                runs
                if runs is not None
                else [{"id": RUN_ID, "org_id": "org-1", "repository_id": REPO_ID}]
            ),
            "repositories": [
                {
                    "id": REPO_ID,
                    "org_id": "org-1",
                    "full_name": "o/r",
                    "merge_policy": setting,
                },
                {
                    "id": OTHER_REPO_ID,
                    "org_id": "org-2",
                    "full_name": "x/y",
                    "merge_policy": "auto",
                },
            ],
        }
        self.fail = set(fail)
        self.queries = []

    def table(self, name):
        return FakeQuery(self, name)

    def repository(self, repo_id=REPO_ID):
        (row,) = [r for r in self.tables["repositories"] if r["id"] == repo_id]
        return row


# --- Resolution matrix -------------------------------------------------------


@pytest.mark.parametrize(
    "ceiling, setting, expected",
    [
        ("human", None, "human"),
        ("human", "human", "human"),
        ("human", "ci", "human"),
        ("human", "auto", "human"),
        ("ci", None, "ci"),
        ("ci", "human", "human"),
        ("ci", "ci", "ci"),
        ("ci", "auto", "ci"),
        ("auto", None, "auto"),
        ("auto", "human", "human"),
        ("auto", "ci", "ci"),
        ("auto", "auto", "auto"),
        # Fail closed on values that should never be stored.
        ("auto", "yolo", "human"),
        ("yolo", None, "human"),
    ],
)
def test_effective_is_the_stricter_of_ceiling_and_setting(ceiling, setting, expected):
    assert effective(ceiling, setting) == expected


@pytest.mark.parametrize(
    "ceiling, allowed",
    [("human", ["human"]), ("ci", ["human", "ci"]), ("auto", ["human", "ci", "auto"])],
)
def test_allowed_settings_never_exceed_the_ceiling(ceiling, allowed):
    assert merge_policy.allowed_settings(ceiling) == allowed
    assert merge_policy.is_allowed(None, ceiling)
    for policy in merge_policy.POLICIES:
        assert merge_policy.is_allowed(policy, ceiling) == (policy in allowed)


@pytest.mark.parametrize(
    "env, expected", [(None, "human"), (" CI ", "ci"), ("auto", "auto"), ("x", "human")]
)
def test_deployment_ceiling_reads_merge_policy(monkeypatch, env, expected):
    if env is None:
        monkeypatch.delenv("MERGE_POLICY", raising=False)
    else:
        monkeypatch.setenv("MERGE_POLICY", env)
    assert merge_policy.deployment_ceiling() == expected
    assert agents.get_merge_policy() == expected


# --- resolve_for_run ---------------------------------------------------------


def test_human_ceiling_needs_no_database_read():
    client = FakeClient(fail={"runs", "repositories"})
    resolution = merge_policy.resolve_for_run(client, RUN_ID, "human")
    assert resolution.policy == "human" and resolution.reasons == []
    assert client.queries == []


def test_without_a_database_the_ceiling_applies():
    resolution = merge_policy.resolve_for_run(None, RUN_ID, "auto")
    assert (resolution.policy, resolution.reasons) == ("auto", [])


def test_repository_setting_lowers_the_ceiling_with_a_reason():
    client = FakeClient(setting="ci")
    resolution = merge_policy.resolve_for_run(client, RUN_ID, "auto")
    assert resolution.policy == "ci" and resolution.setting == "ci"
    assert resolution.reasons == ["repository policy ci (ceiling auto)"]
    # The repository is read within the run's organization.
    (repo_query,) = [q for q in client.queries if q[0] == "repositories"]
    assert ("org_id", "org-1") in repo_query[1] and ("id", REPO_ID) in repo_query[1]


def test_setting_above_the_ceiling_is_never_effective():
    resolution = merge_policy.resolve_for_run(FakeClient(setting="auto"), RUN_ID, "ci")
    assert (resolution.policy, resolution.reasons) == ("ci", [])


@pytest.mark.parametrize(
    "client",
    [
        FakeClient(setting=None),
        FakeClient(runs=[]),  # no run row: no repository to read a setting from
        FakeClient(runs=[{"id": RUN_ID, "org_id": "org-1", "repository_id": None}]),
        FakeClient(runs=[{"id": RUN_ID, "org_id": "org-1", "repository_id": 999}]),
    ],
    ids=["inherit", "no-run-row", "run-without-repository", "no-repository-row"],
)
def test_no_setting_inherits_the_ceiling(client):
    resolution = merge_policy.resolve_for_run(client, RUN_ID, "auto")
    assert (resolution.policy, resolution.reasons) == ("auto", [])


def test_another_orgs_repository_setting_is_not_used():
    # A run row naming another org's repository (which RLS and /start make
    # impossible) does not pick up that repository's setting.
    client = FakeClient(
        runs=[{"id": RUN_ID, "org_id": "org-1", "repository_id": OTHER_REPO_ID}]
    )
    client.tables["repositories"][1]["merge_policy"] = "human"
    assert merge_policy.resolve_for_run(client, RUN_ID, "auto").policy == "auto"


@pytest.mark.parametrize(
    "client",
    [
        FakeClient(fail={"runs"}),
        FakeClient(fail={"repositories"}),
        FakeClient(setting="yolo"),
        MagicMock(),  # a response without a list of rows
    ],
    ids=["runs-error", "repositories-error", "invalid-value", "malformed-response"],
)
def test_unreadable_setting_fails_closed_to_human(client, caplog):
    resolution = merge_policy.resolve_for_run(client, RUN_ID, "auto")
    assert resolution.policy == "human"
    assert resolution.reasons == [merge_policy.REASON_UNREADABLE]
    assert RUN_ID in caplog.text


# --- At the merge decision (agents.maintainer_node) --------------------------


@pytest.fixture
def recorded(monkeypatch):
    token = agents.current_run_id.set(RUN_ID)
    record = MagicMock(return_value=True)
    monkeypatch.setattr(merge_audit, "record_decision", record)
    monkeypatch.setattr(agents, "_load_checkpoint", AsyncMock(return_value={}))
    monkeypatch.setattr(agents, "_save_checkpoint", AsyncMock())
    yield record
    agents.current_run_id.reset(token)


def _node(monkeypatch, ceiling, client, **setup):
    monkeypatch.setenv("MERGE_POLICY", ceiling)
    repo, pr, _ = _setup(monkeypatch, **setup)
    monkeypatch.setattr(agents, "supabase", client)
    return repo, pr


def _state_with():
    return _state() | {"verification": {"status": "passed", "head_sha": "headsha"}}


async def test_repository_human_holds_under_an_auto_ceiling(monkeypatch, recorded):
    _, pr = _node(monkeypatch, "auto", FakeClient(setting="human"))

    result = await agents.maintainer_node(_state_with())

    pr.merge.assert_not_called()
    assert result["merge_status"] == "awaiting_human_review"
    row = recorded.call_args.kwargs
    assert (row["policy"], row["decision"]) == ("human", "held")
    assert row["reasons"] == [
        "maintainer approved",
        "policy human",
        "repository policy human (ceiling auto)",
    ]
    assert any(
        "repository policy human (ceiling auto)" in log.get("msg", "")
        for log in result["log_messages"]
    )


async def test_repository_ci_applies_the_ci_gate_under_an_auto_ceiling(
    monkeypatch, recorded
):
    # No green checks: auto would merge, the repository's ci does not.
    _, pr = _node(monkeypatch, "auto", FakeClient(setting="ci"))

    await agents.maintainer_node(_state_with())

    pr.merge.assert_not_called()
    row = recorded.call_args.kwargs
    assert row["policy"] == "ci" and row["decision"] == "held"
    assert row["reasons"][:3] == [
        "maintainer approved",
        "policy ci",
        "repository policy ci (ceiling auto)",
    ]


async def test_repository_ci_still_merges_with_green_checks(monkeypatch, recorded):
    _, pr = _node(
        monkeypatch,
        "auto",
        FakeClient(setting="ci"),
        statuses=[VERIFIED],
        check_runs=[("tests", "completed", "success")],
    )

    result = await agents.maintainer_node(_state_with())

    assert result["merge_status"] == "merged"
    pr.merge.assert_called_once()
    assert recorded.call_args.kwargs["policy"] == "ci"


async def test_stored_setting_above_the_ceiling_does_not_loosen_it(
    monkeypatch, recorded
):
    _, pr = _node(monkeypatch, "human", FakeClient(setting="auto"))

    await agents.maintainer_node(_state_with())

    pr.merge.assert_not_called()
    assert recorded.call_args.kwargs["policy"] == "human"


async def test_unreadable_setting_holds_the_pr_and_records_why(monkeypatch, recorded):
    _, pr = _node(monkeypatch, "auto", FakeClient(fail={"repositories"}))

    result = await agents.maintainer_node(_state_with())

    pr.merge.assert_not_called()
    assert result["merge_status"] == "awaiting_human_review"
    row = recorded.call_args.kwargs
    assert (row["policy"], row["decision"]) == ("human", "held")
    assert merge_policy.REASON_UNREADABLE in row["reasons"]


async def test_inherited_setting_keeps_the_ceiling(monkeypatch, recorded):
    _, pr = _node(monkeypatch, "auto", FakeClient(setting=None))

    result = await agents.maintainer_node(_state_with())

    assert result["merge_status"] == "merged"
    assert recorded.call_args.kwargs["reasons"] == [
        "maintainer approved",
        "policy auto",
        "trusted trigger",
    ]


async def test_status_post_failure_follows_the_effective_policy(monkeypatch):
    """Under a ci ceiling a failed status post stops the run, unless the
    repository lowered its policy to human (nothing would merge anyway)."""
    import verification
    from verification import VerificationResult

    repo = MagicMock()
    repo.get_branch.return_value.commit.sha = "b" * 40
    repo.get_commit.return_value.create_status.side_effect = RuntimeError("403")
    monkeypatch.setattr(
        verification,
        "verify_pr_branch",
        AsyncMock(return_value=VerificationResult("passed", "ok", head_sha="b" * 40)),
    )
    monkeypatch.setattr(verification, "record_result", MagicMock())
    monkeypatch.setattr(agents, "_run_github_token", lambda: None)
    monkeypatch.setattr(agents, "emit_event", AsyncMock())
    monkeypatch.setenv("MERGE_POLICY", "ci")
    token = agents.current_run_id.set(RUN_ID)
    try:
        monkeypatch.setattr(agents, "supabase", FakeClient(setting="human"))
        log = await agents._verify_and_report(repo, 7, "fix-branch", "calc.py")
        assert "passed" in log["msg"]

        monkeypatch.setattr(agents, "supabase", FakeClient(setting=None))
        with pytest.raises(agents.PipelineAbortError) as exc:
            await agents._verify_and_report(repo, 7, "fix-branch", "calc.py")
        assert exc.value.error_code == "VERIFICATION_STATUS_FAILED"
    finally:
        agents.current_run_id.reset(token)


# --- GET/PUT /repositories/{id}/merge-policy --------------------------------


def _user(role, org_id="org-1"):
    async def user():
        return {"user_id": "u1", "org_id": org_id, "role": role, "email": "e"}

    return user


@pytest.fixture
def api(monkeypatch):
    client = FakeClient(setting=None)
    monkeypatch.setattr(agents, "supabase", client)
    monkeypatch.setenv("MERGE_POLICY", "ci")
    http = TestClient(main.app)
    http.db = client

    def as_role(role, org_id="org-1"):
        main.app.dependency_overrides[main.get_current_user] = _user(role, org_id)
        return http

    http.as_role = as_role
    yield http
    main.app.dependency_overrides.clear()


URL = f"/repositories/{REPO_ID}/merge-policy"


@pytest.mark.parametrize("role", ["owner", "admin", "member", "viewer"])
def test_members_read_the_policy(api, role):
    api.db.repository()["merge_policy"] = "human"

    response = api.as_role(role).get(URL)

    assert response.status_code == 200
    assert response.json() == {
        "repository_id": REPO_ID,
        "full_name": "o/r",
        "setting": "human",
        "ceiling": "ci",
        "effective": "human",
        "allowed": ["human", "ci"],
        "can_edit": role in ("owner", "admin"),
    }


@pytest.mark.parametrize("role", ["owner", "admin"])
@pytest.mark.parametrize("value", ["human", "ci", None])
def test_admins_set_or_clear_the_policy(api, role, value):
    response = api.as_role(role).put(URL, json={"merge_policy": value})

    assert response.status_code == 200
    assert response.json()["setting"] == value
    assert response.json()["effective"] == (value or "ci")
    assert api.db.repository()["merge_policy"] == value
    (update,) = [q for q in api.db.queries if q[2] is not None]
    assert ("org_id", "org-1") in update[1]


@pytest.mark.parametrize("role", ["member", "viewer", None])
def test_non_admins_cannot_change_the_policy(api, role):
    response = api.as_role(role).put(URL, json={"merge_policy": "human"})

    assert response.status_code == 403
    assert api.db.repository()["merge_policy"] is None


def test_setting_looser_than_the_ceiling_is_rejected(api):
    response = api.as_role("owner").put(URL, json={"merge_policy": "auto"})

    assert response.status_code == 422
    assert "MERGE_POLICY (ci)" in response.json()["detail"]
    assert api.db.repository()["merge_policy"] is None


@pytest.mark.parametrize(
    "body", [{}, {"merge_policy": "yolo"}, {"merge_policy": "HUMAN"}, {"x": 1}]
)
def test_invalid_bodies_are_rejected(api, body):
    assert api.as_role("owner").put(URL, json=body).status_code == 422
    assert api.db.repository()["merge_policy"] is None


def test_another_orgs_repository_is_not_found(api):
    other = f"/repositories/{OTHER_REPO_ID}/merge-policy"
    missing = "/repositories/1/merge-policy"
    admin = api.as_role("owner")

    for url in (other, missing):
        assert admin.get(url).status_code == 404
        assert admin.put(url, json={"merge_policy": "human"}).status_code == 404
    assert admin.get(other).json() == admin.get(missing).json()
    assert api.db.repository(OTHER_REPO_ID)["merge_policy"] == "auto"


def test_other_org_admin_cannot_change_this_orgs_repository(api):
    response = api.as_role("owner", org_id="org-2").put(
        URL, json={"merge_policy": "human"}
    )
    assert response.status_code == 404
    assert api.db.repository()["merge_policy"] is None


def test_without_a_database_the_routes_are_unavailable(api, monkeypatch):
    api.as_role("owner")
    monkeypatch.setattr(agents, "supabase", None)
    # get_current_user itself refuses without a database; the override here
    # skips it, so the route's own check answers.
    assert api.get(URL).status_code == 503
    assert api.put(URL, json={"merge_policy": None}).status_code == 503


def test_requires_authentication(monkeypatch):
    monkeypatch.setattr(agents, "supabase", MagicMock())
    monkeypatch.delenv("AUTH_DEV_BYPASS", raising=False)
    http = TestClient(main.app)
    assert http.get(URL).status_code == 401
    assert http.put(URL, json={"merge_policy": "human"}).status_code == 401
