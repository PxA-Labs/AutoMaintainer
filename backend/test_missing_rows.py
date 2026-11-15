"""Lookups of a single row must answer 404/403 when the row is missing (#402).

The double below behaves like postgrest-py: ``.single()`` raises APIError
(PostgREST answers 406) when no row matches, while ``.maybe_single()``
returns ``None`` instead of a response. With the old ``.single()`` calls
every one of these requests failed with a 500.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from postgrest.exceptions import APIError

import agents
import main
from main import app

client = TestClient(app, raise_server_exceptions=False)


class PostgrestLikeQuery:
    def __init__(self, rows):
        self.rows = list(rows)
        self.filters = []
        self.mode = "many"
        self.max_rows = None

    def select(self, *_a, **_k):
        return self

    def order(self, *_a, **_k):
        return self

    def limit(self, n):
        self.max_rows = n
        return self

    def eq(self, col, val):
        self.filters.append((col, val))
        return self

    def single(self):
        self.mode = "single"
        return self

    def maybe_single(self):
        self.mode = "maybe_single"
        return self

    def execute(self):
        rows = [r for r in self.rows if all(r.get(c) == v for c, v in self.filters)]
        if self.max_rows is not None:
            rows = rows[: self.max_rows]
        if self.mode == "single":
            if len(rows) != 1:
                raise APIError(
                    {
                        "message": "Cannot coerce the result to a single JSON object",
                        "code": "PGRST116",
                    }
                )
            return SimpleNamespace(data=rows[0])
        if self.mode == "maybe_single":
            if not rows:
                return None
            return SimpleNamespace(data=rows[0])
        return SimpleNamespace(data=rows)


def supabase_with(tables):
    sb = MagicMock()
    sb.table.side_effect = lambda name: PostgrestLikeQuery(tables.get(name, []))
    # /admin/metrics checks the RPC first and falls back to the table.
    sb.rpc.side_effect = Exception("rpc unavailable")
    return sb


@pytest.fixture(autouse=True)
def as_member():
    async def user():
        return {"user_id": "u1", "org_id": "org-1", "role": "member", "email": "e"}

    app.dependency_overrides[main.get_current_user] = user
    yield
    app.dependency_overrides.clear()


def test_validate_repo_access_unknown_repo_is_404():
    sb = supabase_with({"repositories": []})

    with pytest.raises(HTTPException) as exc:
        asyncio.run(main.validate_repo_access("o/missing", "org-1", "u1", sb))

    assert exc.value.status_code == 404


def test_validate_repo_access_returns_ids_for_known_repo():
    repo = {"id": 1, "github_installation_id": 2, "org_id": "org-1", "full_name": "o/r"}
    sb = supabase_with({"repositories": [repo]})

    assert asyncio.run(main.validate_repo_access("o/r", "org-1", "u1", sb)) == (1, 2)


def test_start_on_unknown_repo_is_404(monkeypatch):
    monkeypatch.setattr(agents, "supabase", supabase_with({"repositories": []}))

    response = client.post("/start", json={"repo_name": "o/missing"})

    assert response.status_code == 404


def test_repo_route_on_unknown_repo_is_404(monkeypatch):
    """require_repo_access goes through validate_repo_access too."""
    monkeypatch.setattr(agents, "supabase", supabase_with({"repositories": []}))

    response = client.get("/repo/o/missing/tree")

    assert response.status_code == 404


def test_status_of_unknown_run_is_404(monkeypatch):
    monkeypatch.setattr(agents, "supabase", supabase_with({"runs": []}))

    response = client.get("/status/run-x")

    assert response.status_code == 404


def test_status_of_other_orgs_run_is_404(monkeypatch):
    run = {"id": "run-x", "org_id": "org-2", "status": "running"}
    monkeypatch.setattr(agents, "supabase", supabase_with({"runs": [run]}))

    response = client.get("/status/run-x")

    assert response.status_code == 404


def test_status_of_own_run_is_returned(monkeypatch):
    run = {"id": "run-x", "org_id": "org-1", "status": "completed"}
    monkeypatch.setattr(agents, "supabase", supabase_with({"runs": [run]}))

    response = client.get("/status/run-x")

    assert response.status_code == 200
    assert response.json()["id"] == "run-x"


def test_stop_unknown_run_is_404(monkeypatch):
    monkeypatch.setattr(agents, "supabase", supabase_with({"runs": []}))

    response = client.post("/stop", json={"run_id": "run-x"})

    assert response.status_code == 404


@pytest.mark.parametrize("path", ["/admin/runs", "/admin/metrics"])
def test_admin_routes_without_membership_row_are_403(monkeypatch, path):
    monkeypatch.setattr(agents, "supabase", supabase_with({}))

    response = client.get(path)

    assert response.status_code == 403
