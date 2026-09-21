from unittest.mock import MagicMock, patch

import pytest

import tasks


def test_worker_rejects_anonymous_key(monkeypatch):
    monkeypatch.setattr(tasks, "task_supabase", None)
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.delenv("SUPABASE_SERVICE_KEY", raising=False)
    monkeypatch.setenv("SUPABASE_ANON_KEY", "anon-key")
    monkeypatch.delenv("SUPABASE_KEY", raising=False)

    with pytest.raises(RuntimeError, match="SUPABASE_SERVICE_KEY is required"):
        tasks.get_supabase()


def test_worker_uses_service_role_key(monkeypatch):
    client = MagicMock()
    monkeypatch.setattr(tasks, "task_supabase", None)
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "service-role-key")

    with patch.object(tasks, "create_client", return_value=client) as create:
        assert tasks.get_supabase() is client

    create.assert_called_once_with("https://example.supabase.co", "service-role-key")
