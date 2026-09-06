from types import SimpleNamespace

import pytest

import agents
from main import get_admin_metrics


class Query:
    def __init__(self, result):
        self.result = result
        self.filters = []

    def select(self, *_columns):
        return self

    def eq(self, column, value):
        self.filters.append((column, value))
        return self

    def order(self, *_args, **_kwargs):
        return self

    def limit(self, *_args, **_kwargs):
        return self

    def gte(self, *_args, **_kwargs):
        return self

    def execute(self):
        return self.result


class Supabase:
    def __init__(self):
        self.queries = {}
        self.responses = {
            "org_health": [
                {
                    "org_id": "org-1",
                    "org_name": "Acme",
                    "slug": "acme",
                    "plan": "team",
                    "member_count": 1,
                    "repo_count": 1,
                    "active_runs": 0,
                    "runs_24h": 0,
                    "failed_24h": 0,
                    "estimated_cost_30d_usd": 0,
                    "last_run_at": None,
                }
            ],
            "recent_runs_detailed": [],
            "monthly_usage_summary": [],
        }

    def rpc(self, *_args, **_kwargs):
        return Query(SimpleNamespace(data=True))

    def table(self, name):
        query = Query(SimpleNamespace(data=self.responses[name]))
        self.queries[name] = query
        return query


@pytest.mark.asyncio
async def test_admin_metrics_filter_every_view_to_current_org(monkeypatch):
    supabase = Supabase()
    monkeypatch.setattr(agents, "supabase", supabase)

    metrics = await get_admin_metrics("org-1", "user-1")

    assert metrics["organizations"][0]["org_id"] == "org-1"
    assert all(
        ("org_id", "org-1") in supabase.queries[name].filters
        for name in ("org_health", "recent_runs_detailed", "monthly_usage_summary")
    )
