"""Tenant isolation for the admin surface.

The backend talks to Supabase with the SERVICE ROLE key, which bypasses Row
Level Security. Tenant scoping is therefore entirely the application's job:
every admin query must filter on the caller's org_id. These tests assert that
filter is actually applied, because RLS will not catch it if it is dropped.
"""

from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

import agents
import main
from main import app, get_org_id, get_user_id

client = TestClient(app)

CALLER_ORG = "org-aaaa-1111"
OTHER_ORG = "org-bbbb-2222"


class RecordingQuery:
    """Minimal supabase-py query stub that records the filters applied."""

    def __init__(self, table_name, log):
        self.table_name = table_name
        self.log = log
        self.filters = {}

    def select(self, *_a, **_kw):
        return self

    def order(self, *_a, **_kw):
        return self

    def limit(self, *_a, **_kw):
        return self

    def gte(self, *_a, **_kw):
        return self

    def eq(self, column, value):
        self.filters[column] = value
        return self

    def execute(self):
        self.log.append((self.table_name, dict(self.filters)))
        return MagicMock(data=[])


@pytest.fixture
def recorded_queries(monkeypatch):
    log = []
    sb = MagicMock()
    sb.table.side_effect = lambda name: RecordingQuery(name, log)
    # The admin role check goes through rpc(), not table().
    sb.rpc.return_value.execute.return_value = MagicMock(data=True)

    monkeypatch.setattr(agents, "supabase", sb, raising=False)
    monkeypatch.setattr(main, "supabase", sb, raising=False)

    app.dependency_overrides[get_org_id] = lambda: CALLER_ORG
    app.dependency_overrides[get_user_id] = lambda: "user-1"
    yield log
    app.dependency_overrides.pop(get_org_id, None)
    app.dependency_overrides.pop(get_user_id, None)


def test_admin_metrics_scopes_every_query_to_caller_org(recorded_queries):
    response = client.get("/admin/metrics")
    assert response.status_code == 200, response.text

    assert recorded_queries, "no queries were issued"

    unscoped = [
        table
        for table, filters in recorded_queries
        if filters.get("org_id") != CALLER_ORG
    ]
    assert not unscoped, (
        f"these admin queries were not scoped to the caller's org: {unscoped}. "
        "The service-role key bypasses RLS, so an unscoped query returns every "
        "tenant's data."
    )


def test_admin_metrics_never_queries_another_org(recorded_queries):
    client.get("/admin/metrics")

    leaked = [
        (table, filters)
        for table, filters in recorded_queries
        if filters.get("org_id") == OTHER_ORG
    ]
    assert not leaked
