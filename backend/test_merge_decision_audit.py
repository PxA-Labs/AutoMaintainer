"""Epic #411: an audit row per merge decision (merge_decisions).

The hook (merge_audit.record_decision), its calls from agents.maintainer_node
on every decision branch, and GET /runs/{run_id}/decisions.
"""

import logging
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient
from github import GithubException

import agents
import main
import merge_audit
from test_agent_merge_policy import VERIFIED, _setup, _state

RUN_ID = "11111111-1111-4111-8111-111111111111"
RUN_ROW = {
    "id": RUN_ID,
    "org_id": "org-1",
    "repository_id": 4242,
    "repo_name": "o/r",
}
PR_URL = "https://github.com/o/r/pull/7"
REJECT = (
    '{"verdict": "reject", "reasons": ["Off-by-one."], '
    '"blocking_issues": ["Off-by-one."]}'
)


class FakeTable:
    def __init__(self, client, name):
        self.client, self.name = client, name
        self.filters = {}

    def _chain(self, *a, **k):
        return self

    select = limit = order = _chain

    def eq(self, col, val):
        self.filters[col] = val
        return self

    def insert(self, row):
        self.inserting = row
        return self

    def execute(self):
        if self.name in self.client.fail:
            raise RuntimeError(f"{self.name} unavailable")
        if hasattr(self, "inserting"):
            self.client.inserted.append((self.name, self.inserting))
            return SimpleNamespace(data=[self.inserting])
        if self.name == "runs":
            rows = [r for r in self.client.runs if r["id"] == self.filters.get("id")]
            return SimpleNamespace(data=rows)
        if self.name == "verification_results":
            rows = [
                r
                for r in self.client.verifications
                if all(r.get(c) == v for c, v in self.filters.items())
            ]
            return SimpleNamespace(data=rows)
        return SimpleNamespace(data=[])


class FakeClient:
    def __init__(self, runs=(RUN_ROW,), fail=(), verifications=()):
        self.runs, self.fail = list(runs), set(fail)
        self.verifications = list(verifications)
        self.inserted = []

    def table(self, name):
        return FakeTable(self, name)


def _record(client, **kw):
    args = dict(
        pr_number=7,
        head_sha="a" * 40,
        iteration=1,
        policy="ci",
        decision="held",
        actor="policy",
        reasons=["maintainer approved", "ci status not success: x"],
        pr_url=PR_URL,
        verification={"status": "failed", "head_sha": "a" * 40, "summary": "s"},
    )
    return merge_audit.record_decision(client, RUN_ID, **(args | kw))


# --- Row shape --------------------------------------------------------------------


def test_row_has_the_run_context_decision_and_evidence():
    client = FakeClient(
        verifications=[{"id": "v-1", "run_id": RUN_ID, "head_sha": "a" * 40}]
    )

    assert _record(client)

    ((table, row),) = client.inserted
    assert table == "merge_decisions"
    assert row == {
        "org_id": "org-1",
        "run_id": RUN_ID,
        "repository_id": 4242,
        "repo_name": "o/r",
        "pr_number": 7,
        "head_sha": "a" * 40,
        "iteration": 1,
        "policy": "ci",
        "decision": "held",
        "actor": "policy",
        "actor_role": None,
        "reasons": ["maintainer approved", "ci status not success: x"],
        "evidence": {
            "pr_url": PR_URL,
            "commit_url": "https://github.com/o/r/commit/" + "a" * 40,
            "verification": {
                "context": "automaintainer/verification",
                "status": "failed",
                "head_sha": "a" * 40,
            },
            "verification_result_id": "v-1",
        },
    }


def test_verification_row_of_another_head_is_not_linked():
    client = FakeClient(
        verifications=[{"id": "v-1", "run_id": RUN_ID, "head_sha": "b" * 40}]
    )
    assert _record(client)
    assert "verification_result_id" not in client.inserted[0][1]["evidence"]


def test_reasons_are_capped_and_stripped_of_nul_bytes():
    client = FakeClient()
    assert _record(client, reasons=["r\x00" + "x" * 500] * 30)

    reasons = client.inserted[0][1]["reasons"]
    assert len(reasons) == merge_audit.MAX_REASONS
    assert all(len(r) == merge_audit.MAX_REASON_CHARS for r in reasons)
    assert all("\x00" not in r for r in reasons)


@pytest.mark.parametrize(
    "pr_url",
    [None, MagicMock(), "javascript:alert(1)", "https://x/" + "y" * 2000],
    ids=["missing", "not-a-string", "not-https", "too-long"],
)
def test_unusable_pr_links_are_left_out(pr_url):
    assert merge_audit.evidence(pr_url=pr_url, head_sha="a" * 40) == {}


def test_unknown_head_records_an_empty_sha_and_no_commit_link():
    client = FakeClient()
    assert _record(client, head_sha=None, verification=None)
    row = client.inserted[0][1]
    assert row["head_sha"] == ""
    assert row["evidence"] == {"pr_url": PR_URL}


# --- Failure tolerance ------------------------------------------------------------


@pytest.mark.parametrize("failing", ["runs", "merge_decisions"])
def test_database_failure_is_logged_with_the_run_id_and_not_raised(failing, caplog):
    client = FakeClient(fail={failing})

    with caplog.at_level(logging.WARNING, logger="merge_audit"):
        assert _record(client) is False

    assert RUN_ID in caplog.text


def test_unreadable_verification_row_still_records_the_decision(caplog):
    client = FakeClient(fail={"verification_results"})

    with caplog.at_level(logging.WARNING, logger="merge_audit"):
        assert _record(client) is True

    assert "verification_result_id" not in client.inserted[0][1]["evidence"]
    assert RUN_ID in caplog.text


def test_run_without_an_organization_row_writes_nothing(caplog):
    client = FakeClient(runs=())

    with caplog.at_level(logging.WARNING, logger="merge_audit"):
        assert _record(client) is False

    assert client.inserted == [] and RUN_ID in caplog.text


@pytest.mark.parametrize("client, run_id", [(None, RUN_ID), (FakeClient(), None)])
def test_no_database_or_no_run_is_a_no_op(client, run_id):
    assert (
        merge_audit.record_decision(
            client,
            run_id,
            pr_number=7,
            head_sha="a",
            iteration=0,
            policy="human",
            decision="held",
            actor="policy",
            reasons=[],
        )
        is False
    )


# --- agents.maintainer_node: one row per decision, on every branch ----------------


@pytest.fixture
def run_id():
    token = agents.current_run_id.set(RUN_ID)
    yield RUN_ID
    agents.current_run_id.reset(token)


@pytest.fixture
def recorded(monkeypatch, run_id):
    record = MagicMock(return_value=True)
    monkeypatch.setattr(merge_audit, "record_decision", record)
    return record


def _node(monkeypatch, policy, **setup):
    monkeypatch.setenv("MERGE_POLICY", policy)
    repo, pr, _ = _setup(monkeypatch, **setup)
    pr.html_url = PR_URL
    return repo, pr


def _only_row(recorded):
    recorded.assert_called_once()
    return recorded.call_args.kwargs


def _state_with(**extra):
    return (
        _state()
        | {
            "verification": {"status": "passed", "head_sha": "headsha"},
        }
        | extra
    )


async def test_auto_merge_records_merged_by_the_policy(monkeypatch, recorded):
    _, pr = _node(monkeypatch, "auto")

    result = await agents.maintainer_node(_state_with())

    assert result["merge_status"] == "merged"
    row = _only_row(recorded)
    assert recorded.call_args.args[1] == RUN_ID
    assert row["policy"] == "auto" and row["decision"] == "merged"
    assert row["actor"] == "policy" and row["actor_role"] is None
    assert row["reasons"] == ["maintainer approved", "policy auto", "trusted trigger"]
    assert row["head_sha"] == "headsha" and row["pr_number"] == 7
    assert row["iteration"] == 0 and row["pr_url"] == PR_URL
    assert row["verification"] == {"status": "passed", "head_sha": "headsha"}


async def test_ci_merge_records_the_checks_that_passed(monkeypatch, recorded):
    _node(
        monkeypatch,
        "ci",
        statuses=[VERIFIED],
        check_runs=[("tests", "completed", "success")],
    )

    result = await agents.maintainer_node(_state_with())

    assert result["merge_status"] == "merged"
    row = _only_row(recorded)
    assert row["decision"] == "merged" and row["policy"] == "ci"
    assert row["reasons"] == [
        "maintainer approved",
        "policy ci",
        "ci checks passed: 2 CI checks passed",
        "trusted trigger",
    ]


async def test_ci_status_not_success_records_held(monkeypatch, recorded):
    _, pr = _node(monkeypatch, "ci", statuses=[("ci/build", "success")])

    result = await agents.maintainer_node(_state_with())

    pr.merge.assert_not_called()
    assert result["merge_status"] == "awaiting_human_review"
    row = _only_row(recorded)
    assert (row["decision"], row["actor"]) == ("held", "policy")
    assert row["reasons"] == [
        "maintainer approved",
        "policy ci",
        "ci status not success: automaintainer/verification did not pass on the PR head",
    ]


@pytest.mark.parametrize("policy", ["human", "yolo"])
async def test_human_policy_never_merges_and_records_held(
    monkeypatch, recorded, policy
):
    _, pr = _node(monkeypatch, policy)

    await agents.maintainer_node(_state_with())

    pr.merge.assert_not_called()
    row = _only_row(recorded)
    # An unknown MERGE_POLICY is "human" in force, and recorded as such.
    assert (row["policy"], row["decision"]) == ("human", "held")
    assert row["reasons"] == ["maintainer approved", "policy human"]


async def test_change_limits_hold_the_pr_and_say_so(monkeypatch, recorded):
    _, pr = _node(monkeypatch, "auto")

    await agents.maintainer_node(
        _state_with(change_limit_violations=["too many lines"])
    )

    pr.merge.assert_not_called()
    row = _only_row(recorded)
    assert row["decision"] == "held"
    assert row["reasons"][-1] == "change limits exceeded"


async def test_unposted_review_comment_holds_the_pr(monkeypatch, recorded):
    _, pr = _node(monkeypatch, "auto")
    pr.create_issue_comment.side_effect = GithubException(502, {}, None)

    await agents.maintainer_node(_state_with())

    row = _only_row(recorded)
    assert row["decision"] == "held"
    assert row["reasons"][-1] == "review comment not posted"


async def test_incomplete_diff_holds_the_pr(monkeypatch, recorded):
    _, pr = _node(monkeypatch, "auto")
    pr.get_files.return_value = [
        SimpleNamespace(filename="app.py", status="modified", patch=None)
    ]

    await agents.maintainer_node(_state_with())

    row = _only_row(recorded)
    assert row["decision"] == "held"
    assert row["reasons"][-1] == "reviewed diff incomplete"


@pytest.mark.parametrize("policy", ["human", "ci", "auto"])
async def test_maintainer_rejection_records_not_merged_by_the_agent(
    monkeypatch, recorded, policy
):
    _, pr = _node(monkeypatch, policy, review=REJECT, statuses=[VERIFIED])

    result = await agents.maintainer_node(_state_with())

    pr.merge.assert_not_called()
    assert result["merge_status"] == "changes_requested"
    row = _only_row(recorded)
    assert row["policy"] == policy
    assert (row["decision"], row["actor"]) == ("not_merged", "agent")
    assert row["actor_role"] == "Maintainer"
    assert row["reasons"] == ["maintainer rejected"]
    assert row["head_sha"] == "headsha"


async def test_invalid_maintainer_reply_is_recorded_as_a_fail_closed_rejection(
    monkeypatch, recorded
):
    _node(monkeypatch, "auto", review="not json at all")

    await agents.maintainer_node(_state_with())

    row = _only_row(recorded)
    assert row["decision"] == "not_merged"
    assert row["reasons"] == [
        "maintainer rejected",
        "maintainer reply invalid (fail closed)",
    ]


async def test_merge_refused_by_github_records_not_merged_then_fails(
    monkeypatch, recorded
):
    _, pr = _node(monkeypatch, "auto")
    pr.merge.side_effect = GithubException(405, {"message": "Not mergeable"}, None)

    with pytest.raises(agents.PipelineAbortError) as exc:
        await agents.maintainer_node(_state_with())

    assert exc.value.error_code == "MERGE_FAILED"
    row = _only_row(recorded)
    assert row["decision"] == "not_merged"
    assert row["reasons"][-1] == "github did not merge the pr"


async def test_no_pull_request_means_no_merge_decision(monkeypatch, recorded):
    monkeypatch.setenv("MERGE_POLICY", "auto")
    _setup(monkeypatch)
    monkeypatch.setattr(agents, "gh", None)

    await agents.maintainer_node(_state_with())

    recorded.assert_not_called()


# --- Failure tolerance in the node ------------------------------------------------


@pytest.mark.parametrize(
    "policy, review, expected",
    [
        ("auto", None, "merged"),
        ("human", None, "awaiting_human_review"),
        ("auto", REJECT, "changes_requested"),
    ],
)
async def test_audit_failure_never_changes_the_decision(
    monkeypatch, run_id, policy, review, expected, caplog
):
    setup = {"review": review} if review else {}
    _, pr = _node(monkeypatch, policy, **setup)
    client = FakeClient(fail={"merge_decisions"})
    monkeypatch.setattr(agents, "supabase", client)
    monkeypatch.setattr(agents, "_load_checkpoint", AsyncMock(return_value={}))
    monkeypatch.setattr(agents, "_save_checkpoint", AsyncMock())

    with caplog.at_level(logging.WARNING, logger="merge_audit"):
        result = await agents.maintainer_node(_state_with())

    assert result["merge_status"] == expected
    assert pr.merge.call_count == (1 if expected == "merged" else 0)
    assert RUN_ID in caplog.text


async def test_node_writes_the_row_through_the_service_client(monkeypatch, run_id):
    _node(monkeypatch, "human")
    client = FakeClient()
    monkeypatch.setattr(agents, "supabase", client)
    monkeypatch.setattr(agents, "_load_checkpoint", AsyncMock(return_value={}))
    monkeypatch.setattr(agents, "_save_checkpoint", AsyncMock())

    await agents.maintainer_node(_state_with())

    (row,) = [r for t, r in client.inserted if t == "merge_decisions"]
    assert (row["org_id"], row["decision"], row["policy"]) == ("org-1", "held", "human")
    assert row["evidence"]["pr_url"] == PR_URL


# --- Redelivered runs -------------------------------------------------------------


async def test_replayed_review_records_nothing(monkeypatch, recorded):
    """A resumed run whose checkpoint has this iteration's outcome replays it:
    no new decision, no new row."""
    _, pr = _node(monkeypatch, "auto")
    monkeypatch.setattr(
        agents,
        "_load_checkpoint",
        AsyncMock(
            return_value={
                "reviewed_iteration": 0,
                "merge_status": "merged",
                "review": "{}",
                "next_iteration": 0,
            }
        ),
    )

    result = await agents.maintainer_node(_state_with())

    assert result["merge_status"] == "merged"
    recorded.assert_not_called()
    pr.merge.assert_not_called()


async def test_resumed_run_that_reviews_again_records_a_second_row(
    monkeypatch, recorded
):
    """A run that crashed after merging but before its checkpoint reviews and
    decides again on redelivery: that is a new decision event, so a new row,
    which says the PR was already merged."""
    _, pr = _node(monkeypatch, "auto")
    await agents.maintainer_node(_state_with())
    pr.merged = True  # the first attempt merged, then crashed

    result = await agents.maintainer_node(_state_with())

    assert result["merge_status"] == "merged"
    pr.merge.assert_called_once()
    assert recorded.call_count == 2
    first, second = (c.kwargs for c in recorded.call_args_list)
    assert first["decision"] == second["decision"] == "merged"
    assert second["reasons"][-1] == "already merged by an earlier attempt"


async def test_each_review_iteration_is_its_own_decision(monkeypatch, recorded):
    _, pr = _node(monkeypatch, "human", review=REJECT)
    await agents.maintainer_node(_state_with())

    async def approve(system, user):
        return '{"verdict": "approve", "reasons": ["Fixed."]}'

    monkeypatch.setattr(agents, "run_llm_with_tools", approve)
    await agents.maintainer_node(_state_with(iteration=1))

    rows = [c.kwargs for c in recorded.call_args_list]
    assert [(r["iteration"], r["decision"]) for r in rows] == [
        (0, "not_merged"),
        (1, "held"),
    ]


# --- GET /runs/{run_id}/decisions -------------------------------------------------


class FakeQuery:
    def __init__(self, rows):
        self.rows = rows
        self.filters = []
        self.ordering = None
        self.single = False
        self.n = None

    def select(self, *a, **k):
        return self

    def eq(self, col, val):
        self.filters.append((col, val))
        return self

    def order(self, col, desc=False):
        self.ordering = (col, desc)
        return self

    def limit(self, n):
        self.n = n
        return self

    def maybe_single(self):
        self.single = True
        return self

    def execute(self):
        rows = [r for r in self.rows if all(r.get(c) == v for c, v in self.filters)]
        if self.ordering:
            col, desc = self.ordering
            rows.sort(key=lambda r: r[col], reverse=desc)
        rows = rows[: self.n]
        if self.single:
            return SimpleNamespace(data=rows[0] if rows else None)
        return SimpleNamespace(data=rows)


OTHER_RUN = "22222222-2222-4222-8222-222222222222"


def _tables(decisions):
    return {
        "runs": [
            {"id": RUN_ID, "org_id": "org-1"},
            {"id": OTHER_RUN, "org_id": "org-2"},
        ],
        "merge_decisions": decisions,
    }


DECISIONS = [
    {"id": "d1", "run_id": RUN_ID, "org_id": "org-1", "created_at": "2026-01-01"},
    {"id": "d3", "run_id": RUN_ID, "org_id": "org-1", "created_at": "2026-01-03"},
    {"id": "d2", "run_id": RUN_ID, "org_id": "org-1", "created_at": "2026-01-02"},
    # A row whose org disagrees with its run is never returned.
    {"id": "leak", "run_id": RUN_ID, "org_id": "org-2", "created_at": "2026-01-05"},
    {"id": "x", "run_id": OTHER_RUN, "org_id": "org-2", "created_at": "2026-01-04"},
]


@pytest.fixture
def api(monkeypatch):
    tables = _tables(list(DECISIONS))
    sb = MagicMock()
    sb.table.side_effect = lambda name: FakeQuery(tables[name])
    monkeypatch.setattr(agents, "supabase", sb)

    async def user():
        return {"user_id": "u1", "org_id": "org-1", "role": "viewer", "email": "e"}

    main.app.dependency_overrides[main.get_current_user] = user
    client = TestClient(main.app)
    client.tables = tables
    yield client
    main.app.dependency_overrides.clear()


def test_lists_the_runs_decisions_newest_first(api):
    response = api.get(f"/runs/{RUN_ID}/decisions")

    assert response.status_code == 200
    body = response.json()
    assert body["run_id"] == RUN_ID
    assert [r["id"] for r in body["decisions"]] == ["d3", "d2", "d1"]


def test_returns_at_most_100_rows(api):
    api.tables["merge_decisions"][:] = [
        {"id": f"d{i}", "run_id": RUN_ID, "org_id": "org-1", "created_at": f"{i:04}"}
        for i in range(150)
    ]

    rows = api.get(f"/runs/{RUN_ID}/decisions").json()["decisions"]

    assert len(rows) == 100 and rows[0]["id"] == "d149"


def test_another_orgs_run_is_not_found_like_status(api):
    listed = api.get(f"/runs/{OTHER_RUN}/decisions")
    status = api.get(f"/status/{OTHER_RUN}")

    assert listed.status_code == status.status_code == 404
    assert listed.json() == status.json()


def test_unknown_run_is_not_found(api):
    assert api.get(f"/runs/{uuid.uuid4()}/decisions").status_code == 404


def test_malformed_run_id_is_rejected(api):
    assert api.get("/runs/not-a-uuid/decisions").status_code == 422


def test_without_a_database_the_route_is_unavailable(api, monkeypatch):
    monkeypatch.setattr(agents, "supabase", None)
    assert api.get(f"/runs/{RUN_ID}/decisions").status_code == 503


def test_requires_authentication(monkeypatch):
    monkeypatch.setattr(agents, "supabase", MagicMock())
    monkeypatch.delenv("AUTH_DEV_BYPASS", raising=False)

    response = TestClient(main.app).get(f"/runs/{RUN_ID}/decisions")

    assert response.status_code == 401
