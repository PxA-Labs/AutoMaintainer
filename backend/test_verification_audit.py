"""Epic #411: an audit row per verification (verification_results).

The hook (verification.record_result), its call from agents._verify_and_report
on both Implementer paths, and GET /runs/{run_id}/verifications.
"""

import logging
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

import agents
import main
import verification
from test_agent_idempotency import CODE, FakeRepo
from verification import VerificationResult

RUN_ID = "11111111-1111-4111-8111-111111111111"
RUN_ROW = {
    "id": RUN_ID,
    "org_id": "org-1",
    "repository_id": 4242,
    "repo_name": "o/r",
    "checkpoint": {"implemented_iteration": 2, "pr_number": 7},
}


class FakeTable:
    def __init__(self, client, name):
        self.client, self.name = client, name
        self.filters = {}

    def _chain(self, *a, **k):
        return self

    select = limit = _chain

    def eq(self, col, val):
        self.filters[col] = val
        return self

    def insert(self, row):
        self.client.inserted.append((self.name, row))
        return self

    def execute(self):
        if self.name in self.client.fail:
            raise RuntimeError(f"{self.name} unavailable")
        if self.name == "runs":
            rows = [r for r in self.client.runs if r["id"] == self.filters.get("id")]
            return SimpleNamespace(data=rows)
        return SimpleNamespace(data=[])


class FakeClient:
    def __init__(self, runs=(RUN_ROW,), fail=()):
        self.runs, self.fail = list(runs), set(fail)
        self.inserted = []

    def table(self, name):
        return FakeTable(self, name)


def _result(**kw):
    defaults = dict(
        status="failed",
        summary="tests failed (exit code 1)",
        command=["python", "-m", "pytest", "-q"],
        duration_seconds=1.234,
        exit_code=1,
        output_tail="FAILED test_x",
        runner="local",
        head_sha="a" * 40,
    )
    return VerificationResult(**(defaults | kw))


# --- Row shape --------------------------------------------------------------------


def test_row_has_the_run_context_and_the_result():
    client = FakeClient()

    assert verification.record_result(client, RUN_ID, _result(), pr_number=7)

    ((table, row),) = client.inserted
    assert table == "verification_results"
    assert row == {
        "org_id": "org-1",
        "run_id": RUN_ID,
        "repository_id": 4242,
        "repo_name": "o/r",
        "pr_number": 7,
        "head_sha": "a" * 40,
        "iteration": 2,
        "status": "failed",
        "summary": "tests failed (exit code 1)",
        "command": ["python", "-m", "pytest", "-q"],
        "exit_code": 1,
        "duration_ms": 1234,
        "output_tail": "FAILED test_x",
        "output_truncated": False,
        "backend": "local",
    }


def test_sandbox_rows_name_the_configured_backend(monkeypatch):
    monkeypatch.setenv("SANDBOX_BACKEND", "runners.docker_sandbox:create")
    row = verification.audit_row(
        _result(runner="sandbox"), RUN_ROW, pr_number=7, head_sha=None
    )
    assert row["backend"] == "runners.docker_sandbox:create"


def test_unresolved_head_falls_back_to_the_branch_head_then_empty():
    skipped = VerificationResult("skipped", "verification disabled")
    row = verification.audit_row(skipped, RUN_ROW, pr_number=7, head_sha="b" * 40)
    assert row["head_sha"] == "b" * 40
    assert row["command"] == [] and row["backend"] is None
    assert row["exit_code"] is None and row["duration_ms"] == 0

    row = verification.audit_row(skipped, RUN_ROW, pr_number=None, head_sha=None)
    assert row["head_sha"] == ""


def test_output_is_cut_to_its_tail_and_stripped_of_nul_bytes():
    long_output = "x" * 10_000 + "\x00END"
    row = verification.audit_row(
        _result(output_tail=long_output, summary="s\x00" * 900),
        RUN_ROW,
        pr_number=7,
    )
    assert len(row["output_tail"]) == verification.AUDIT_OUTPUT_TAIL_CHARS
    assert row["output_tail"].endswith("xEND") and "\x00" not in row["output_tail"]
    assert row["output_truncated"] is True
    assert "\x00" not in row["summary"]
    assert len(row["summary"]) <= verification.AUDIT_SUMMARY_CHARS


def test_runner_side_truncation_is_kept():
    row = verification.audit_row(_result(truncated=True), RUN_ROW, pr_number=7)
    assert row["output_truncated"] is True


# --- Failure tolerance ------------------------------------------------------------


@pytest.mark.parametrize("failing", ["runs", "verification_results"])
def test_database_failure_is_logged_with_the_run_id_and_not_raised(failing, caplog):
    client = FakeClient(fail={failing})

    with caplog.at_level(logging.WARNING, logger="verification"):
        assert verification.record_result(client, RUN_ID, _result()) is False

    assert RUN_ID in caplog.text


def test_run_without_an_organization_row_writes_nothing(caplog):
    client = FakeClient(runs=())

    with caplog.at_level(logging.WARNING, logger="verification"):
        assert verification.record_result(client, RUN_ID, _result()) is False

    assert client.inserted == [] and RUN_ID in caplog.text


@pytest.mark.parametrize("client, run_id", [(None, RUN_ID), (FakeClient(), None)])
def test_no_database_or_no_run_is_a_no_op(client, run_id):
    assert verification.record_result(client, run_id, _result()) is False


# --- agents._verify_and_report ----------------------------------------------------


def _gh_repo(post_status_error=None):
    repo = MagicMock()
    repo.clone_url = "https://github.com/o/r.git"
    repo.get_branch.return_value.commit.sha = "b" * 40
    commit = MagicMock()
    if post_status_error:
        commit.create_status.side_effect = post_status_error
    repo.get_commit.return_value = commit
    return repo, commit


@pytest.fixture
def run_id():
    token = agents.current_run_id.set(RUN_ID)
    yield RUN_ID
    agents.current_run_id.reset(token)


@pytest.fixture
def verify_env(monkeypatch):
    monkeypatch.setattr(agents, "emit_event", AsyncMock())
    monkeypatch.setattr(agents, "_run_github_token", lambda: None)

    def use(result):
        monkeypatch.setattr(
            verification, "verify_pr_branch", AsyncMock(return_value=result)
        )

    return use


@pytest.mark.parametrize("status", ["passed", "failed"])
async def test_audit_failure_changes_nothing_about_the_verification(
    monkeypatch, run_id, verify_env, status, caplog
):
    result = VerificationResult(status, "s", head_sha="b" * 40, exit_code=0)
    verify_env(result)
    monkeypatch.setenv("MERGE_POLICY", "ci")
    monkeypatch.setattr(agents, "supabase", FakeClient(fail={"verification_results"}))
    repo, commit = _gh_repo()

    with caplog.at_level(logging.WARNING, logger="verification"):
        log = await agents._verify_and_report(repo, 7, "fix-branch", "calc.py")

    assert log.result is result and log.result.status == status
    commit.create_status.assert_called_once()
    expected_state = "success" if status == "passed" else "failure"
    assert commit.create_status.call_args.kwargs["state"] == expected_state
    assert RUN_ID in caplog.text


async def test_row_is_written_even_when_github_then_aborts_the_run(
    monkeypatch, run_id, verify_env
):
    verify_env(VerificationResult("passed", "ok", head_sha="b" * 40))
    monkeypatch.setenv("MERGE_POLICY", "ci")
    client = FakeClient()
    monkeypatch.setattr(agents, "supabase", client)
    repo, _ = _gh_repo(post_status_error=RuntimeError("403"))

    with pytest.raises(agents.PipelineAbortError):
        await agents._verify_and_report(repo, 7, "fix-branch", "calc.py")

    ((_, row),) = client.inserted
    assert row["status"] == "passed" and row["pr_number"] == 7


async def test_skipped_verification_records_the_branch_head(
    monkeypatch, run_id, verify_env
):
    verify_env(VerificationResult("skipped", "verification disabled"))
    client = FakeClient()
    monkeypatch.setattr(agents, "supabase", client)
    repo, _ = _gh_repo()

    await agents._verify_and_report(repo, 7, "fix-branch", "calc.py")

    ((_, row),) = client.inserted
    assert row["head_sha"] == "b" * 40 and row["status"] == "skipped"


async def test_reverifying_the_same_head_writes_a_new_row(
    monkeypatch, run_id, verify_env
):
    verify_env(VerificationResult("passed", "ok", head_sha="b" * 40))
    client = FakeClient()
    monkeypatch.setattr(agents, "supabase", client)
    repo, _ = _gh_repo()

    await agents._verify_and_report(repo, 7, "fix-branch", "calc.py")
    await agents._verify_and_report(repo, 7, "fix-branch", "calc.py")

    assert len(client.inserted) == 2


# --- Both Implementer paths -------------------------------------------------------


@pytest.fixture
def repo(monkeypatch):
    repo = FakeRepo()
    monkeypatch.setattr(agents, "gh", type("GH", (), {"get_repo": lambda s, n: repo})())
    monkeypatch.setattr(agents, "supabase", None)
    monkeypatch.setattr(agents, "emit_event", AsyncMock())
    monkeypatch.setattr(agents, "_run_github_token", lambda: None)
    monkeypatch.setattr(agents, "run_llm_with_tools", AsyncMock(return_value=CODE))
    return repo


def _impl_state():
    return {
        "repo_name": "o/r",
        "idea": "set x to 2 in app.py:1-1",
        "issue_number": 1,
        "iteration": 0,
        "review": "",
        "target_file_path": "app.py",
    }


@pytest.fixture
def recorded(monkeypatch):
    record = MagicMock(return_value=True)
    monkeypatch.setattr(verification, "record_result", record)
    return record


async def test_implementer_records_the_verification(repo, monkeypatch, recorded):
    result = VerificationResult("failed", "tests failed (exit code 1)", exit_code=1)
    monkeypatch.setattr(
        verification, "verify_pr_branch", AsyncMock(return_value=result)
    )

    await agents.implementer_node(_impl_state())

    recorded.assert_called_once()
    _client, _run_id, recorded_result = recorded.call_args.args
    assert recorded_result is result
    assert recorded.call_args.kwargs["pr_number"] == 1


async def test_resumed_implementer_records_the_reverification(
    repo, monkeypatch, recorded
):
    repo.branches["b"] = "head"
    repo.files[("b", "app.py")] = CODE
    repo.create_pull("t", "Closes #1", "b", "main")
    monkeypatch.setattr(
        agents,
        "_load_checkpoint",
        AsyncMock(
            return_value={
                "implemented_iteration": 0,
                "pr_number": 1,
                "branch_name": "b",
                "target_file_path": "app.py",
            }
        ),
    )
    result = VerificationResult("error", "sandbox failed")
    monkeypatch.setattr(
        verification, "verify_pr_branch", AsyncMock(return_value=result)
    )

    await agents.implementer_node(_impl_state())

    recorded.assert_called_once()
    assert recorded.call_args.args[2] is result
    assert recorded.call_args.kwargs["pr_number"] == 1


# --- GET /runs/{run_id}/verifications ---------------------------------------------


class FakeQuery:
    def __init__(self, rows):
        self.rows = rows
        self.filters = []
        self.ordering = None
        self.single = False

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
TABLES = {
    "runs": [
        {"id": RUN_ID, "org_id": "org-1"},
        {"id": OTHER_RUN, "org_id": "org-2"},
    ],
    "verification_results": [
        {"id": "v1", "run_id": RUN_ID, "org_id": "org-1", "created_at": "2026-01-01"},
        {"id": "v3", "run_id": RUN_ID, "org_id": "org-1", "created_at": "2026-01-03"},
        {"id": "v2", "run_id": RUN_ID, "org_id": "org-1", "created_at": "2026-01-02"},
        {"id": "x", "run_id": OTHER_RUN, "org_id": "org-2", "created_at": "2026-01-04"},
    ],
}


@pytest.fixture
def api(monkeypatch):
    sb = MagicMock()
    sb.table.side_effect = lambda name: FakeQuery(TABLES[name])
    monkeypatch.setattr(agents, "supabase", sb)

    async def user():
        return {"user_id": "u1", "org_id": "org-1", "role": "viewer", "email": "e"}

    main.app.dependency_overrides[main.get_current_user] = user
    yield TestClient(main.app)
    main.app.dependency_overrides.clear()


def test_lists_the_runs_rows_newest_first(api):
    response = api.get(f"/runs/{RUN_ID}/verifications")

    assert response.status_code == 200
    body = response.json()
    assert body["run_id"] == RUN_ID
    assert [r["id"] for r in body["verifications"]] == ["v3", "v2", "v1"]


def test_another_orgs_run_is_not_found_like_status(api):
    listed = api.get(f"/runs/{OTHER_RUN}/verifications")
    status = api.get(f"/status/{OTHER_RUN}")

    assert listed.status_code == status.status_code == 404
    assert listed.json() == status.json()


def test_unknown_run_is_not_found(api):
    assert api.get(f"/runs/{uuid.uuid4()}/verifications").status_code == 404


def test_malformed_run_id_is_rejected(api):
    assert api.get("/runs/not-a-uuid/verifications").status_code == 422


def test_requires_authentication(monkeypatch):
    monkeypatch.setattr(agents, "supabase", MagicMock())
    monkeypatch.delenv("AUTH_DEV_BYPASS", raising=False)

    response = TestClient(main.app).get(f"/runs/{RUN_ID}/verifications")

    assert response.status_code == 401
