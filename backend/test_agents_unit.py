import pytest
import asyncio
import httpx
from unittest.mock import MagicMock
from agents import (
    broadcast_log,
    extract_target_file,
    get_all_groq_keys,
    normalize_generated_code,
    should_implement,
    should_iterate,
)
from workspace import get_base_workspace_dir, get_safe_repo_dir, get_safe_target_path
from fastapi import HTTPException


def test_extract_target_file_accepts_repository_relative_reference():
    directive = "Update backend/main.py:141-161 and preserve unrelated code."
    assert extract_target_file(directive) == "backend/main.py"


def test_extract_target_file_rejects_traversal_reference():
    assert extract_target_file("Update ../secrets.env:1-2") is None


def test_normalize_generated_code_removes_fences_and_rejects_errors():
    assert normalize_generated_code("```python\nprint('ok')\n```") == "print('ok')"
    with pytest.raises(ValueError, match="empty code"):
        normalize_generated_code("```python\n```")
    with pytest.raises(RuntimeError, match="LLM failed"):
        normalize_generated_code("[ERROR] LLM failed")


@pytest.mark.asyncio
async def test_implementer_updates_target_file_instead_of_creating_dummy_file(
    monkeypatch,
):
    import agents

    repo = MagicMock()
    repo.default_branch = "main"
    repo.get_branch.return_value.commit.sha = "base-sha"
    source_file = MagicMock()
    source_file.path = "backend/main.py"
    source_file.sha = "file-sha"
    source_file.decoded_content = b"def old_handler():\n    return 1\n"
    repo.get_contents.return_value = source_file
    repo.create_pull.return_value.number = 42
    repo.create_pull.return_value.html_url = "https://github.com/example/repo/pull/42"
    monkeypatch.setattr(agents, "gh", MagicMock(get_repo=MagicMock(return_value=repo)))

    prompts = {}

    async def fake_llm(system_prompt, user_prompt):
        prompts["system"] = system_prompt
        prompts["user"] = user_prompt
        return "```python\ndef new_handler():\n    return 2\n```"

    monkeypatch.setattr(agents, "run_llm_with_tools", fake_llm)
    state = {
        "repo_name": "example/repo",
        "idea": "Modify backend/main.py:141-161 to fix the handler.",
        "issue_number": 12,
        "iteration": 0,
        "review": "",
        "branch_name": "",
        "target_file_path": "",
        "pr_number": 0,
    }

    result = await agents.implementer_node(state)

    repo.update_file.assert_called_once()
    assert repo.update_file.call_args.kwargs["path"] == "backend/main.py"
    assert repo.update_file.call_args.kwargs["sha"] == "file-sha"
    repo.create_file.assert_not_called()
    assert result["target_file_path"] == "backend/main.py"
    assert "Current file contents" in prompts["user"]


@pytest.mark.asyncio
async def test_implementer_creates_explicit_missing_target_file(monkeypatch):
    import agents
    from github import GithubException

    repo = MagicMock()
    repo.default_branch = "main"
    repo.get_branch.return_value.commit.sha = "base-sha"
    repo.get_contents.side_effect = GithubException(404, "not found")
    repo.create_pull.return_value.number = 43
    repo.create_pull.return_value.html_url = "https://github.com/example/repo/pull/43"
    monkeypatch.setattr(agents, "gh", MagicMock(get_repo=MagicMock(return_value=repo)))

    async def fake_llm(system_prompt, user_prompt):
        assert "Create the referenced repository file" in system_prompt
        assert "does not exist yet" in user_prompt
        return "```python\nprint('new file')\n```"

    monkeypatch.setattr(agents, "run_llm_with_tools", fake_llm)
    state = {
        "repo_name": "example/repo",
        "idea": "Create backend/new_feature.py:1-4 for the missing feature.",
        "issue_number": 13,
        "iteration": 0,
        "review": "",
        "branch_name": "",
        "target_file_path": "",
        "pr_number": 0,
    }

    result = await agents.implementer_node(state)

    repo.update_file.assert_not_called()
    repo.create_file.assert_called_once()
    assert repo.create_file.call_args.kwargs["path"] == "backend/new_feature.py"
    assert result["target_file_path"] == "backend/new_feature.py"


def test_get_all_groq_keys(monkeypatch):
    # Test with GROQ_API_KEY and additional numbered keys
    monkeypatch.setenv("GROQ_API_KEY", "primary-key")
    monkeypatch.setenv("GROQ_API_KEY_1", "key-1")
    monkeypatch.setenv("GROQ_API_KEY_2", "key-2")
    monkeypatch.delenv("GROQ_API_KEY_3", raising=False)

    keys = get_all_groq_keys()
    assert "primary-key" in keys
    assert "key-1" in keys
    assert "key-2" in keys
    assert len(keys) >= 3


def test_should_implement():
    # PM Decision APPROVED -> implementer
    state_approved = {"pm_decision": "  APPROVED with fixes "}
    assert should_implement(state_approved) == "implementer"

    # PM Decision REJECTED -> END
    state_rejected = {"pm_decision": "REJECTED"}
    assert should_implement(state_rejected) == "__end__"


def test_should_iterate():
    # LGTM -> END
    state_lgtm = {"review": "Looks good to me. LGTM!", "iteration": 0}
    assert should_iterate(state_lgtm) == "__end__"

    # No LGTM, iteration < 3 -> implementer
    state_continue = {"review": "Need more changes", "iteration": 1}
    assert should_iterate(state_continue) == "implementer"

    # No LGTM, iteration >= 3 -> END
    state_limit = {"review": "Still need changes", "iteration": 3}
    assert should_iterate(state_limit) == "__end__"


@pytest.mark.asyncio
async def test_broadcast_log_no_supabase():
    # If supabase is None, broadcast_log should gracefully fallback and not raise error
    import agents

    agents.supabase = None

    # This should run without raising any Exception
    await broadcast_log({"msg": "Test fallback logging", "type": "message"})


def test_healthz_supabase_not_configured(monkeypatch):
    import agents

    monkeypatch.setattr(agents, "supabase", None)

    from fastapi.testclient import TestClient
    from main import app

    client = TestClient(app)

    response = client.get("/healthz/supabase")
    assert response.status_code == 503
    assert "not configured" in response.json()["detail"]


def test_healthz_supabase_healthy(monkeypatch):
    import agents

    mock_client = MagicMock()
    mock_table = MagicMock()
    mock_select = MagicMock()
    mock_limit = MagicMock()
    mock_execute = MagicMock()

    mock_client.table.return_value = mock_table
    mock_table.select.return_value = mock_select
    mock_select.limit.return_value = mock_limit
    mock_limit.execute.return_value = mock_execute

    monkeypatch.setattr(agents, "supabase", mock_client)

    from fastapi.testclient import TestClient
    from main import app

    client = TestClient(app)

    response = client.get("/healthz/supabase")
    assert response.status_code == 200
    assert response.json()["status"] == "healthy"


def test_healthz_supabase_unhealthy(monkeypatch):
    import agents

    mock_client = MagicMock()
    mock_table = MagicMock()
    mock_select = MagicMock()
    mock_limit = MagicMock()

    mock_client.table.return_value = mock_table
    mock_table.select.return_value = mock_select
    mock_select.limit.return_value = mock_limit
    mock_limit.execute.side_effect = Exception("Database paused")

    monkeypatch.setattr(agents, "supabase", mock_client)

    from fastapi.testclient import TestClient
    from main import app

    client = TestClient(app)

    response = client.get("/healthz/supabase")
    assert response.status_code == 503
    assert "connection failed" in response.json()["detail"]


def test_workspace_helpers(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOMAINTAINER_WORKSPACE_DIR", str(tmp_path))
    base = get_base_workspace_dir()
    assert base == tmp_path

    repo_dir = get_safe_repo_dir("PxA-Labs/AutoMaintainer")
    assert repo_dir.name == "PxA-Labs_AutoMaintainer"
    assert repo_dir.parent.exists()

    with pytest.raises(HTTPException):
        get_safe_repo_dir("../../etc/passwd")

    target = get_safe_target_path(repo_dir, "src/index.py")
    assert target.is_relative_to(repo_dir)

    with pytest.raises(HTTPException):
        get_safe_target_path(repo_dir, "../../../secret.txt")

    # Test symlink escape attempt
    repo_dir.mkdir(parents=True, exist_ok=True)
    outside_dir = tmp_path / "outside_dir"
    outside_dir.mkdir(parents=True, exist_ok=True)
    outside_file = outside_dir / "secret.txt"
    outside_file.write_text("classified", encoding="utf-8")

    symlink_dir = repo_dir / "symlink_dir"
    try:
        symlink_dir.symlink_to(outside_dir, target_is_directory=True)
        with pytest.raises(HTTPException):
            get_safe_target_path(repo_dir, "symlink_dir/secret.txt")
    except OSError:
        # On Windows without developer mode, symlink creation may raise OSError
        pass


@pytest.mark.asyncio
async def test_start_and_stop_concurrency(monkeypatch):
    import main

    async def mock_agent_loop(repo, issue, run_id):
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            pass

    monkeypatch.setattr(main, "run_agent_loop", mock_agent_loop)

    async def allow_repository_access():
        return {"org_id": "test-org", "user_id": "test-user"}

    async def allow_current_user():
        return {"org_id": "test-org", "user_id": "test-user"}

    monkeypatch.setitem(
        main.app.dependency_overrides,
        main.require_repository_access,
        allow_repository_access,
    )
    monkeypatch.setitem(
        main.app.dependency_overrides,
        main.get_current_user,
        allow_current_user,
    )

    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        res_start = await client.post("/start-legacy", json={"repo_name": "test/repo"})
        assert res_start.status_code == 200
        run_id = res_start.json()["run_id"]

        assert run_id in main.active_tasks

        res_stop = await client.post("/stop-legacy", json={"run_id": run_id})
        assert res_stop.status_code == 200
        assert res_stop.json()["status"] == "stopped"

        res_stop_again = await client.post("/stop-legacy", json={"run_id": run_id})
        assert res_stop_again.status_code == 200
        assert res_stop_again.json()["status"] == "not_running"
