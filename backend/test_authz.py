"""Authorization hardening: memberships, roles, dev bypass, pagination, and
what error responses are allowed to reveal."""

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import agents
import main
from main import app

client = TestClient(app)


class FakeQuery:
    """PostgREST query double that applies .eq() and .not_.is_(col, "null")."""

    def __init__(self, rows):
        self.rows = rows
        self.filters = []
        self.negate = False

    def _chain(self, *a, **k):
        return self

    select = order = limit = maybe_single = single = _chain

    def eq(self, col, val):
        self.filters.append(lambda r: r.get(col) == val)
        return self

    @property
    def not_(self):
        self.negate = True
        return self

    def is_(self, col, val):
        assert val == "null"
        negate, self.negate = self.negate, False
        self.filters.append(lambda r: (r.get(col) is None) != negate)
        return self

    def execute(self):
        rows = [r for r in self.rows if all(f(r) for f in self.filters)]
        return SimpleNamespace(data=rows[0] if rows else None)


def supabase_with_members(rows):
    sb = MagicMock()
    sb.auth.get_user.return_value = SimpleNamespace(
        user=SimpleNamespace(id="u1", email="u1@example.com")
    )
    sb.table.side_effect = lambda name: FakeQuery(rows)
    return sb


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    monkeypatch.delenv("AUTH_DEV_BYPASS", raising=False)
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    yield
    app.dependency_overrides.clear()


def as_role(role):
    """Authenticate every request as a member of org-1 with ``role``."""

    async def user():
        return {"user_id": "u1", "org_id": "org-1", "role": role, "email": "e"}

    app.dependency_overrides[main.get_current_user] = user
    app.dependency_overrides[main.require_repo_access] = lambda: "org-1"


# --- Pending invites ---------------------------------------------------------


def test_pending_invite_is_not_membership(monkeypatch):
    """joined_at NULL is an unaccepted invite and must not grant org access."""
    pending = {"user_id": "u1", "org_id": "org-victim", "role": "admin"}
    pending["joined_at"] = None
    monkeypatch.setattr(agents, "supabase", supabase_with_members([pending]))

    with pytest.raises(HTTPException) as exc:
        asyncio.run(main.get_current_user(authorization="Bearer tok"))

    assert exc.value.status_code == 403


def test_accepted_membership_resolves_org_and_role(monkeypatch):
    member = {"user_id": "u1", "org_id": "org-1", "role": "viewer", "joined_at": "t"}
    monkeypatch.setattr(agents, "supabase", supabase_with_members([member]))

    user = asyncio.run(main.get_current_user(authorization="Bearer tok"))

    assert user["org_id"] == "org-1"
    assert user["role"] == "viewer"


# --- AUTH_DEV_BYPASS -----------------------------------------------------------


def test_dev_bypass_ignored_in_production(monkeypatch):
    monkeypatch.setattr(agents, "supabase", MagicMock())
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")

    with pytest.raises(HTTPException) as exc:
        asyncio.run(main.get_current_user(authorization=None))
    assert exc.value.status_code == 401

    websocket = SimpleNamespace(scope={"subprotocols": []})
    assert asyncio.run(main.authenticate_websocket(websocket)) is None


def test_dev_bypass_refuses_to_start_in_production(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "Production")
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")

    async def start():
        async with main.lifespan(app):
            pass

    with pytest.raises(RuntimeError, match="AUTH_DEV_BYPASS"):
        asyncio.run(start())


def test_dev_bypass_still_works_in_development(monkeypatch):
    monkeypatch.setattr(agents, "supabase", MagicMock())
    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")

    user = asyncio.run(main.get_current_user(authorization=None))

    assert user["email"] == "dev@localhost"


# --- Roles -------------------------------------------------------------------


@pytest.fixture
def github(monkeypatch, tmp_path):
    monkeypatch.setenv("AUTOMAINTAINER_WORKSPACE_DIR", str(tmp_path))
    monkeypatch.setenv("GITHUB_TOKEN", "fake")
    repo = MagicMock()
    repo.get_contents.return_value = SimpleNamespace(path="a.py", sha="s")
    repo.default_branch = "main"
    # IDE writes target an unprotected working branch, never "main" (#139).
    repo.get_branch.return_value = SimpleNamespace(protected=False)
    monkeypatch.setattr(main, "Github", lambda *_: MagicMock(get_repo=lambda _: repo))
    return repo


VIEWER_BLOCKED = [
    ("post", "/repo/o/r/file", {"json": {"file_path": "a.py", "content": "x"}}),
    (
        "post",
        "/repo/o/r/file",
        {"json": {"file_path": ".github/workflows/x.yml", "content": "x"}},
    ),
    ("post", "/repo/o/r/file/create", {"json": {"file_path": "b.py"}}),
    ("delete", "/repo/o/r/file?file_path=a.py", {}),
    (
        "post",
        "/repo/o/r/propose-changes",
        {"json": {"title": "t", "changes": [{"path": "a.py", "content": "x"}]}},
    ),
    ("post", "/start", {"json": {"repo_name": "o/r"}}),
    ("post", "/stop", {"json": {"run_id": "run-1"}}),
    ("post", "/stop", {}),
]


@pytest.mark.parametrize("method,path,kwargs", VIEWER_BLOCKED)
@pytest.mark.parametrize("role", ["viewer", None, "unknown"])
def test_read_only_roles_cannot_write(monkeypatch, github, role, method, path, kwargs):
    """A viewer could commit any file (including workflows) with the server PAT."""
    monkeypatch.setattr(agents, "supabase", MagicMock())
    as_role(role)

    response = getattr(client, method)(path, **kwargs)

    assert response.status_code == 403, response.text
    github.update_file.assert_not_called()
    github.create_file.assert_not_called()
    github.delete_file.assert_not_called()
    github.create_git_ref.assert_not_called()


def test_member_can_update_file(monkeypatch, github):
    monkeypatch.setattr(agents, "supabase", MagicMock())
    as_role("member")

    response = client.post("/repo/o/r/file", json={"file_path": "a.py", "content": "x"})

    assert response.status_code == 200, response.text
    github.update_file.assert_called_once()


@pytest.fixture
def running_runs(monkeypatch):
    sb = MagicMock()
    query = sb.table.return_value.select.return_value.eq.return_value.in_.return_value
    query.execute.return_value = SimpleNamespace(data=[{"id": "r1"}, {"id": "r2"}])
    monkeypatch.setattr(agents, "supabase", sb)
    cancelled = []

    def cancel(run_id):
        cancelled.append(run_id)
        return {"success": True}

    monkeypatch.setattr(main, "cancel_run_task", cancel)
    return cancelled


def test_bulk_stop_requires_admin(running_runs):
    """/stop without run_id cancels every run in the org; members may not."""
    as_role("member")

    response = client.post("/stop")

    assert response.status_code == 403
    assert running_runs == []


@pytest.mark.parametrize("role", ["admin", "owner"])
def test_admin_can_bulk_stop(running_runs, role):
    as_role(role)

    response = client.post("/stop")

    assert response.status_code == 200, response.text
    assert response.json() == {"status": "cancelled", "count": 2}
    assert running_runs == ["r1", "r2"]


# --- IDE writes validate the local path before committing --------------------


@pytest.mark.parametrize(
    "path,body",
    [
        ("/repo/o/r/file", {"file_path": ".git/config", "content": "x"}),
        ("/repo/o/r/file/create", {"file_path": ".git/hooks/post-merge"}),
        ("/repo/o/r/file/create", {"file_path": ".git", "is_dir": True}),
    ],
)
def test_ide_write_validates_local_path_before_github(
    monkeypatch, github, tmp_path, path, body
):
    """A path the workspace refuses used to get an upstream commit, then a 500."""
    from workspace import get_safe_repo_dir

    monkeypatch.setattr(agents, "supabase", MagicMock())
    as_role("member")
    (get_safe_repo_dir("o/r") / ".git").mkdir(parents=True)

    response = client.post(path, json=body)

    assert response.status_code == 403, response.text
    github.update_file.assert_not_called()
    github.create_file.assert_not_called()


def test_ide_local_mirror_failure_does_not_fail_committed_change(
    monkeypatch, github, tmp_path
):
    """Once GitHub has the commit, a local write error is logged, not a 500."""
    from workspace import get_safe_repo_dir

    monkeypatch.setattr(agents, "supabase", MagicMock())
    as_role("member")
    repo_dir = get_safe_repo_dir("o/r")
    repo_dir.mkdir(parents=True)
    # A directory where the file should go makes write_text fail.
    (repo_dir / "a.py").mkdir()

    response = client.post("/repo/o/r/file", json={"file_path": "a.py", "content": "x"})

    assert response.status_code == 200, response.text
    github.update_file.assert_called_once()


# --- Error responses do not echo exception text ------------------------------


SECRET_ERROR = "ghp_leakedtoken db.internal.example:5432"


def test_github_failure_detail_is_generic(monkeypatch, github):
    monkeypatch.setattr(agents, "supabase", MagicMock())
    as_role("member")
    github.update_file.side_effect = Exception(SECRET_ERROR)

    response = client.post("/repo/o/r/file", json={"file_path": "a.py", "content": "x"})

    assert response.status_code == 500
    detail = response.json()["detail"]
    assert "ghp_" not in detail and "internal" not in detail
    assert "error id:" in detail


def test_repo_not_found_detail_is_generic(monkeypatch, tmp_path):
    monkeypatch.setenv("AUTOMAINTAINER_WORKSPACE_DIR", str(tmp_path))
    monkeypatch.setenv("GITHUB_TOKEN", "fake")
    monkeypatch.setattr(agents, "supabase", MagicMock())
    as_role("member")

    def get_repo(_):
        raise Exception(SECRET_ERROR)

    monkeypatch.setattr(main, "Github", lambda *_: MagicMock(get_repo=get_repo))

    response = client.delete("/repo/o/r/file?file_path=a.py")

    assert response.status_code == 404
    assert "ghp_" not in response.text


def test_start_queue_failure_detail_is_generic(monkeypatch):
    sb = MagicMock()
    repo_q = sb.table.return_value.select.return_value.eq.return_value.eq.return_value
    repo_q.limit.return_value.maybe_single.return_value.execute.return_value = (
        SimpleNamespace(data={"id": 1, "github_installation_id": 2})
    )
    monkeypatch.setattr(agents, "supabase", sb)

    def explode(*a, **k):
        raise Exception(SECRET_ERROR)

    monkeypatch.setattr(
        main, "run_agent_loop_task", SimpleNamespace(apply_async=explode)
    )

    async def worker_available():
        return True

    monkeypatch.setattr(main, "agent_worker_available", worker_available)
    as_role("member")

    response = client.post("/start", json={"repo_name": "o/r"})

    assert response.status_code == 500
    assert "ghp_" not in response.text


def test_healthz_supabase_does_not_leak_host_or_exception(monkeypatch):
    sb = MagicMock()
    sb.table.return_value.select.return_value.limit.return_value.execute.side_effect = (
        Exception(SECRET_ERROR)
    )
    monkeypatch.setattr(agents, "supabase", sb)
    monkeypatch.setattr(
        agents, "SUPABASE_URL", "https://secretproject.supabase.co", raising=False
    )

    response = client.get("/healthz/supabase")

    assert response.status_code == 503
    detail = response.json()["detail"]
    assert "connection failed" in detail
    assert "secretproject" not in detail and "ghp_" not in detail


# --- Pagination bounds -------------------------------------------------------


@pytest.mark.parametrize("path", ["/runs", "/admin/runs"])
@pytest.mark.parametrize(
    "limit,offset", [(-5, 0), (0, 0), (101, 0), (10**9, 0), (2**70, 0), (50, -100)]
)
def test_runs_reject_out_of_range_pagination(monkeypatch, path, limit, offset):
    sb = MagicMock()
    monkeypatch.setattr(agents, "supabase", sb)
    as_role("admin")

    response = client.get(path, params={"limit": limit, "offset": offset})

    assert response.status_code == 422
    sb.table.return_value.select.return_value.eq.return_value.order.return_value.range.assert_not_called()


@pytest.mark.parametrize("path", ["/runs", "/admin/runs"])
def test_runs_accept_in_range_pagination(monkeypatch, path):
    sb = MagicMock()
    runs_q = (
        sb.table.return_value.select.return_value.eq.return_value.order.return_value
    )
    runs_q.range.return_value.execute.return_value = SimpleNamespace(data=[])
    admin_q = sb.table.return_value.select.return_value.eq.return_value.eq.return_value
    admin_q.limit.return_value.maybe_single.return_value.execute.return_value = (
        SimpleNamespace(data={"role": "admin"})
    )
    monkeypatch.setattr(agents, "supabase", sb)
    as_role("admin")

    response = client.get(path, params={"limit": 100, "offset": 20})

    assert response.status_code == 200, response.text
    runs_q.range.assert_called_once_with(20, 119)
