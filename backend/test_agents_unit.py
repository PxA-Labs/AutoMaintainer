import pytest
import asyncio
import httpx
from unittest.mock import MagicMock
from agents import get_all_groq_keys, should_implement, should_iterate, broadcast_log
from workspace import get_base_workspace_dir, get_safe_repo_dir, get_safe_target_path
from fastapi import HTTPException


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

    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        res_start = await client.post("/start", json={"repo_name": "test/repo"})
        assert res_start.status_code == 200
        run_id = res_start.json()["run_id"]

        assert run_id in main.active_tasks

        res_stop = await client.post("/stop", json={"run_id": run_id})
        assert res_stop.status_code == 200
        assert res_stop.json()["status"] == "stopped"

        res_stop_again = await client.post("/stop", json={"run_id": run_id})
        assert res_stop_again.status_code == 200
        assert res_stop_again.json()["status"] == "not_running"
