from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import github_app
import tasks


@pytest.mark.asyncio
async def test_sync_installation_repositories_upserts_active_repositories():
    supabase = MagicMock()
    upsert_method = MagicMock()
    upsert_result = MagicMock()
    upsert_method.return_value = upsert_result
    upsert_result.execute.return_value = SimpleNamespace(data=[])
    supabase.table.return_value.upsert = upsert_method

    repos = [
        {
            "id": 42,
            "full_name": "acme/app",
            "name": "app",
            "owner_login": "acme",
            "owner_type": "Organization",
            "private": True,
            "default_branch": "main",
            "description": "App",
            "language": "Python",
            "topics": [],
            "avatar_url": "https://example.test/avatar.png",
            "html_url": "https://github.com/acme/app",
            "archived": False,
            "disabled": False,
            "pushed_at": None,
        },
        {
            "id": 43,
            "full_name": "acme/archived",
            "name": "archived",
            "owner_login": "acme",
            "owner_type": "Organization",
            "private": False,
            "default_branch": "main",
            "description": None,
            "language": None,
            "topics": [],
            "avatar_url": "",
            "html_url": "",
            "archived": True,
            "disabled": False,
            "pushed_at": None,
        },
    ]

    with patch.object(
        github_app, "get_installation_repositories", new=AsyncMock(return_value=repos)
    ):
        synced = await github_app.sync_installation_repositories(supabase, 7, "org-1")

    assert synced == 1
    record = upsert_method.call_args.args[0]
    assert record["id"] == 42
    assert record["org_id"] == "org-1"
    assert record["github_installation_id"] == 7
    assert isinstance(record["synced_at"], str)
    upsert_method.assert_called_once_with(record, on_conflict="id")
    upsert_result.execute.assert_called_once_with()


def test_sync_repositories_calls_installation_sync():
    supabase = MagicMock()
    query = supabase.table.return_value.select.return_value.is_.return_value
    query.execute.return_value = SimpleNamespace(
        data=[
            {"id": 7, "org_id": "org-1", "account_login": "acme"},
            {"id": 8, "org_id": "org-2", "account_login": "other"},
        ]
    )

    with patch.object(tasks, "get_supabase", return_value=supabase), patch.object(
        tasks, "sync_installation_repositories", side_effect=[2, 3]
    ) as sync:
        task = tasks.sync_repositories
        result = task.run() if hasattr(task, "run") else task(None)

    assert result == {
        "installations_processed": 2,
        "repositories_synced": 5,
        "errors": [],
    }
    assert sync.call_args_list[0].args == (supabase, 7, "org-1")
    assert sync.call_args_list[1].args == (supabase, 8, "org-2")
