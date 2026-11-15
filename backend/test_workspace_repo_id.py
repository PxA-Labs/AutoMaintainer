"""The agent run and the IDE/file/terminal routes key a repository's clone the
same way: by GitHub's repository id (``repositories.id`` = ``runs.repository_id``)
when one is known, else by the name hash (issue #392).

Supabase and GitHub are faked; nothing here calls a real service.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

import agents
import main
import tasks
import workspace
from events import NullEmitter
from main import app
from test_tenant_tokens import fake_db

client = TestClient(app)

ORG = "org-a"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOMAINTAINER_WORKSPACE_DIR", str(tmp_path / "ws"))
    monkeypatch.delenv("AUTH_DEV_BYPASS", raising=False)
    yield
    app.dependency_overrides.clear()


def use_repositories(monkeypatch, *rows):
    sb, _ = fake_db(repositories=[dict(r) for r in rows])
    monkeypatch.setattr(agents, "supabase", sb)


def repo_row(full_name, repo_id):
    return {
        "id": repo_id,
        "org_id": ORG,
        "full_name": full_name,
        "github_installation_id": 7,
    }


def as_member():
    async def user():
        return {"user_id": "u", "org_id": ORG, "role": "member", "email": "e"}

    # Only authentication is faked: require_repo_access runs for real and does
    # the org-scoped repositories lookup.
    app.dependency_overrides[main.get_current_user] = user


async def agent_clone_dir(monkeypatch, repo_name, repository_id):
    """The directory the architect node syncs for this run state."""
    seen = []

    async def fake_sync(name, repo_dir, token, **_kw):
        seen.append(repo_dir)
        return True

    async def fake_exec(*_a, **_k):
        proc = MagicMock()

        async def communicate():
            return (b"", b"")

        proc.communicate = communicate
        return proc

    async def llm(_system, _user):
        return "Fix app.py:1-2"

    monkeypatch.setattr(workspace, "sync_repo_clone", fake_sync)
    monkeypatch.setattr(agents.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(agents, "gh", None)
    monkeypatch.setattr(agents, "run_llm_with_tools", llm)
    await agents.architect_node(
        {"repo_name": repo_name, "target_issue": None, "repository_id": repository_id}
    )
    assert len(seen) == 1
    return seen[0]


def route_reads(repo_name, file_path="app.py"):
    return client.get(f"/repo/{repo_name}/file", params={"file_path": file_path})


# --- agent and routes agree ----------------------------------------------------


@pytest.mark.asyncio
async def test_agent_and_ide_routes_use_the_same_clone(monkeypatch):
    use_repositories(monkeypatch, repo_row("o/r", 101))
    as_member()

    agent_dir = await agent_clone_dir(monkeypatch, "o/r", 101)
    assert agent_dir == str(workspace.get_safe_repo_dir("o/r", 101))

    # What the agent wrote is what the IDE reads.
    workspace.Path(agent_dir).mkdir(parents=True, exist_ok=True)
    (workspace.Path(agent_dir) / "app.py").write_text("x = 1\n", encoding="utf-8")

    r = route_reads("o/r")
    assert r.status_code == 200
    assert r.json() == {"content": "x = 1\n"}
    tree = client.get("/repo/o/r/tree")
    assert tree.status_code == 200
    assert [c["name"] for c in tree.json()["children"]] == ["app.py"]
    search = client.get("/repo/o/r/search", params={"q": "x = 1"})
    assert search.status_code == 200
    assert search.json()["results"][0]["file"] == "app.py"


@pytest.mark.asyncio
async def test_terminal_cwd_is_the_agents_clone(monkeypatch):
    use_repositories(monkeypatch, repo_row("o/r", 101))
    agent_dir = await agent_clone_dir(monkeypatch, "o/r", 101)
    workspace.Path(agent_dir).mkdir(parents=True, exist_ok=True)

    allowed, cwd = await main._authorize_terminal_cwd(
        "https://github.com/o/r", {"org_id": ORG, "user_id": "u"}
    )
    assert allowed
    assert cwd == main.os.path.realpath(agent_dir)


def test_file_write_mirrors_into_the_id_keyed_clone(monkeypatch):
    use_repositories(monkeypatch, repo_row("o/r", 101))
    monkeypatch.setenv("GITHUB_AUTH_MODE", "pat")
    monkeypatch.setenv("GITHUB_TOKEN", "pat-token")

    async def user():
        return {"user_id": "u", "org_id": ORG, "role": "member", "email": "e"}

    app.dependency_overrides[main.get_current_user] = user
    gh_repo = MagicMock()
    gh_repo.get_contents.return_value = SimpleNamespace(path="app.py", sha="s")
    monkeypatch.setattr(
        main, "Github", lambda _token: MagicMock(get_repo=lambda _n: gh_repo)
    )
    clone = workspace.get_safe_repo_dir("o/r", 101)
    clone.mkdir(parents=True, exist_ok=True)

    r = client.post("/repo/o/r/file", json={"file_path": "app.py", "content": "y"})
    assert r.status_code == 200
    assert (clone / "app.py").read_text(encoding="utf-8") == "y"
    r = client.post(
        "/repo/o/r/file/create", json={"file_path": "new.py", "content": "z"}
    )
    assert r.status_code == 200
    assert (clone / "new.py").read_text(encoding="utf-8") == "z"
    # Nothing went to the name-keyed directory.
    assert not workspace.get_safe_repo_dir("o/r").joinpath("app.py").exists()


# --- rename / recreate ----------------------------------------------------------


@pytest.mark.asyncio
async def test_renamed_repo_keeps_its_clone(monkeypatch):
    # The agent cloned the repository under its old name ...
    agent_dir = await agent_clone_dir(monkeypatch, "o/old-name", 101)
    workspace.Path(agent_dir).mkdir(parents=True, exist_ok=True)
    (workspace.Path(agent_dir) / "app.py").write_text("old\n", encoding="utf-8")

    # ... and after the rename (same id) the IDE finds that same clone.
    use_repositories(monkeypatch, repo_row("o/new-name", 101))
    as_member()
    r = route_reads("o/new-name")
    assert r.status_code == 200
    assert r.json() == {"content": "old\n"}
    assert await agent_clone_dir(monkeypatch, "o/new-name", 101) == agent_dir


@pytest.mark.asyncio
async def test_recreated_repo_does_not_see_the_old_clone(monkeypatch):
    old_dir = await agent_clone_dir(monkeypatch, "o/r", 101)
    workspace.Path(old_dir).mkdir(parents=True, exist_ok=True)
    (workspace.Path(old_dir) / "app.py").write_text("old repo\n", encoding="utf-8")

    # Deleted and recreated under the same name: GitHub assigns a new id.
    use_repositories(monkeypatch, repo_row("o/r", 202))
    as_member()
    new_dir = await agent_clone_dir(monkeypatch, "o/r", 202)
    assert new_dir != old_dir
    assert route_reads("o/r").status_code == 404


# --- fallback without an id ----------------------------------------------------


@pytest.mark.asyncio
async def test_without_an_id_both_sides_keep_the_name_hash_directory(monkeypatch):
    name_dir = workspace.get_safe_repo_dir("o/r")
    assert name_dir.name.startswith("o_r-")
    assert workspace.REPO_ID_DIR not in name_dir.parts

    # Agent run without a database row (local dev).
    assert await agent_clone_dir(monkeypatch, "o/r", None) == str(name_dir)

    # A route whose require_repo_access did not run has no id either.
    app.dependency_overrides[main.require_repo_access] = lambda: ORG
    name_dir.mkdir(parents=True, exist_ok=True)
    (name_dir / "app.py").write_text("named\n", encoding="utf-8")
    r = route_reads("o/r")
    assert r.status_code == 200
    assert r.json() == {"content": "named\n"}


def test_route_id_is_only_used_for_the_repository_it_was_looked_up_for():
    request = SimpleNamespace(state=SimpleNamespace())
    main._remember_repository_id(request, "o/r", 101)
    assert main._repo_dir_for_request(request, "o/r") == (
        workspace.get_safe_repo_dir("o/r", 101)
    )
    assert main._repo_dir_for_request(request, "o/other") == (
        workspace.get_safe_repo_dir("o/other")
    )


# --- the id reaches the run ------------------------------------------------------


def test_celery_task_passes_repository_id_to_the_real_run_agent_loop():
    kwargs = tasks._agent_loop_kwargs(
        repo_name="o/r",
        target_issue=None,
        run_id="r",
        github_installation_id=9,
        repository_id=101,
    )
    assert kwargs["repository_id"] == 101


def test_execute_agent_run_passes_the_runs_repository_id(monkeypatch):
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
    task = SimpleNamespace(request=SimpleNamespace(id="celery-1", retries=0))

    tasks._execute_agent_run(
        task,
        run_id="run-1",
        repo_name="o/r",
        org_id=ORG,
        user_id="u",
        repository_id=101,
        github_installation_id=7,
    )
    assert seen["repository_id"] == 101
    assert seen["github_installation_id"] == 7


@pytest.mark.asyncio
async def test_run_agent_loop_puts_the_repository_id_in_graph_state(monkeypatch):
    seen = []

    class Graph:
        async def astream(self, state, stream_mode=None):
            seen.append(state.get("repository_id"))
            yield {**state, "log_messages": []}

    monkeypatch.setattr(agents, "supabase", None)
    monkeypatch.setattr(agents, "app", Graph())
    monkeypatch.setenv("GITHUB_AUTH_MODE", "pat")
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    await agents.run_agent_loop(
        "o/r", run_id="r1", emitter=NullEmitter(), repository_id=101
    )
    await agents.run_agent_loop("o/r", run_id="r2", emitter=NullEmitter())
    assert seen == [101, None]
