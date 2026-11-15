"""Tests for the docker compose smoke test (#396).

The smoke test itself (scripts/compose-smoke.sh) needs Docker and runs in the
compose-smoke workflow. These tests cover what can be checked without it:

- the fake PostgREST server (scripts/compose-smoke/fake_supabase.py) answers
  the real supabase-py client the way the API and the worker rely on;
- the flow the script asserts -- /start queues a run, the worker marks it
  running and it ends "failed" with "GitHub credentials unavailable" -- holds
  in-process against that fake, with only Redis and the broker stubbed out;
- the override, the script and the workflow keep their safety properties
  (dev bypass confined to the test override, real credentials never read,
  pinned actions, read-only token, teardown on exit).
"""

import importlib.util
import os
import re
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import yaml

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

REPO_ROOT = Path(__file__).resolve().parents[2]
SMOKE_DIR = REPO_ROOT / "scripts" / "compose-smoke"
OVERRIDE = SMOKE_DIR / "compose.smoke.yml"
SCRIPT = REPO_ROOT / "scripts" / "compose-smoke.sh"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "compose-smoke.yml"

DEV_ORG_ID = "00000000-0000-0000-0000-000000000002"  # main._dev_bypass_user default
SMOKE_REPO = "smoke-test/fixture-repo"


def _load_fake_supabase():
    spec = importlib.util.spec_from_file_location(
        "fake_supabase", SMOKE_DIR / "fake_supabase.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _ComposeLoader(yaml.SafeLoader):
    """SafeLoader that accepts Compose's merge tags (!reset, !override)."""


def _tagged(loader, node):
    if isinstance(node, yaml.SequenceNode):
        value = loader.construct_sequence(node)
    elif isinstance(node, yaml.MappingNode):
        value = loader.construct_mapping(node)
    else:
        value = loader.construct_scalar(node)
    return {"__tag__": node.tag, "value": value}


_ComposeLoader.add_constructor("!reset", _tagged)
_ComposeLoader.add_constructor("!override", _tagged)


def _override() -> dict:
    # _ComposeLoader subclasses yaml.SafeLoader and only adds two tag
    # constructors, so this cannot instantiate arbitrary objects.
    return yaml.load(  # nosec B506
        OVERRIDE.read_text(encoding="utf-8"), Loader=_ComposeLoader
    )


def _seed_from_override() -> dict:
    import json

    env = _override()["services"]["fake-supabase"]["environment"]
    return json.loads(env["FAKE_SUPABASE_SEED"])


@pytest.fixture
def fake_db():
    """The fake PostgREST server on an ephemeral port, seeded like the override."""
    fake = _load_fake_supabase()
    server = fake.make_server("127.0.0.1", 0, _seed_from_override())
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def sb(fake_db):
    from supabase import create_client

    return create_client(fake_db[1], "smoke-test-placeholder-not-a-secret")


# --- fake_supabase.py speaks the supabase-py dialect -------------------------


def test_fake_answers_the_repository_lookup_of_start(sb):
    found = (
        sb.table("repositories")
        .select("id, github_installation_id")
        .eq("org_id", DEV_ORG_ID)
        .eq("full_name", SMOKE_REPO)
        .limit(1)
        .maybe_single()
        .execute()
    )
    assert found.data["id"] == 4242
    assert found.data["github_installation_id"] == 4242

    missing = (
        sb.table("repositories")
        .select("id")
        .eq("org_id", "another-org")
        .eq("full_name", SMOKE_REPO)
        .limit(1)
        .maybe_single()
        .execute()
    )
    assert missing is None


def test_fake_insert_update_and_upsert_merge(sb):
    sb.table("runs").insert(
        {"id": "r1", "org_id": DEV_ORG_ID, "status": "queued"}
    ).execute()
    sb.table("runs").update({"status": "running"}).eq("id", "r1").execute()
    # run_agent_loop upserts a partial row; it must merge, not drop org_id.
    sb.table("runs").upsert(
        {"id": "r1", "status": "running", "repo_name": SMOKE_REPO}, on_conflict="id"
    ).execute()

    row = (
        sb.table("runs")
        .select("*")
        .eq("id", "r1")
        .eq("org_id", DEV_ORG_ID)
        .limit(1)
        .maybe_single()
        .execute()
        .data
    )
    assert row == {
        "id": "r1",
        "org_id": DEV_ORG_ID,
        "status": "running",
        "repo_name": SMOKE_REPO,
    }


def test_fake_assigns_ids_and_rejects_duplicates(sb):
    from postgrest.exceptions import APIError

    created = sb.table("logs").insert({"run_id": "r1", "message": "hi"}).execute()
    assert created.data[0]["id"]

    sb.table("runs").insert({"id": "dup"}).execute()
    with pytest.raises(APIError):
        sb.table("runs").insert({"id": "dup"}).execute()


def test_fake_filters_and_delete(sb):
    sb.table("runs").insert(
        [
            {"id": "a", "status": "queued", "error": None},
            {"id": "b", "status": "failed", "error": "x"},
        ]
    ).execute()
    assert [
        r["id"]
        for r in sb.table("runs").select("*").neq("status", "failed").execute().data
    ] == ["a"]
    assert [
        r["id"]
        for r in sb.table("runs").select("*").in_("status", ["failed"]).execute().data
    ] == ["b"]
    assert [
        r["id"]
        for r in sb.table("runs").select("*").is_("error", "null").execute().data
    ] == ["a"]

    sb.table("runs").delete().eq("id", "a").execute()
    assert [r["id"] for r in sb.table("runs").select("*").execute().data] == ["b"]


def test_fake_fails_loudly_on_unsupported_operators(sb):
    from postgrest.exceptions import APIError

    with pytest.raises(APIError, match="does not support"):
        sb.table("runs").select("*").lt("updated_at", "2026-01-01").execute()


# --- the flow compose-smoke.sh asserts, in-process ---------------------------


class _FakeTask:
    def __init__(self):
        self.request = SimpleNamespace(id="celery-smoke-1", retries=0)

    def retry(self, **kwargs):  # pragma: no cover - must not happen here
        raise AssertionError(f"unexpected retry: {kwargs}")


@pytest.fixture
def smoke_env(monkeypatch, sb):
    """The environment compose.smoke.yml gives the api and worker."""
    import agents
    import github_app
    import main
    import tasks

    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    monkeypatch.setenv("GITHUB_AUTH_MODE", "app")
    for name in ("AUTH_DEV_USER_ID", "AUTH_DEV_ORG_ID", "AUTH_DEV_ROLE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(github_app, "GITHUB_APP_ID", None)
    monkeypatch.setattr(github_app, "GITHUB_APP_PRIVATE_KEY", None)
    monkeypatch.setattr(github_app, "GITHUB_APP_PRIVATE_KEY_PATH", None)

    monkeypatch.setattr(agents, "supabase", sb)
    monkeypatch.setattr(tasks, "task_supabase", sb)

    # Redis-backed pieces; the compose stack has a real Redis for these.
    monkeypatch.setattr(tasks, "acquire_repo_lock", lambda *a, **k: True)
    monkeypatch.setattr(tasks, "release_repo_lock", lambda *a, **k: None)
    monkeypatch.setattr(tasks, "write_heartbeat", lambda *a, **k: None)

    async def worker_available():
        return True

    monkeypatch.setattr(main, "agent_worker_available", worker_available)
    queued = MagicMock()
    queued.apply_async.return_value = SimpleNamespace(id="celery-smoke-1")
    monkeypatch.setattr(main, "run_agent_loop_task", queued)
    return SimpleNamespace(main=main, tasks=tasks, queued=queued)


def test_run_goes_from_queued_to_failed_without_credentials(smoke_env, fake_db):
    from fastapi.testclient import TestClient

    client = TestClient(smoke_env.main.app)
    store = fake_db[0].store

    started = client.post("/start", json={"repo_name": SMOKE_REPO})
    assert started.status_code == 200, started.text
    run_id = started.json()["run_id"]
    assert store.tables["runs"][0]["status"] == "queued"
    assert store.tables["runs"][0]["org_id"] == DEV_ORG_ID

    # What the Celery worker does with the message /start enqueued.
    call = smoke_env.queued.apply_async.call_args
    assert call.kwargs["queue"] == "agent_runs"
    result = smoke_env.tasks._execute_agent_run(
        _FakeTask(), *call.kwargs["args"], **call.kwargs["kwargs"]
    )
    assert result["status"] == "failed"
    assert "GitHub credentials unavailable" in result["error"]

    status = client.get(f"/status/{run_id}")
    assert status.status_code == 200, status.text
    row = status.json()
    # The same assertions compose-smoke.sh makes on GET /status.
    assert row["status"] == "failed"
    assert "GitHub credentials unavailable" in row["error_message"]
    assert row["started_at"], "the worker never marked the run running"
    assert row["celery_task_id"] == "celery-smoke-1"


# --- static safety checks ----------------------------------------------------


def test_override_confines_dev_bypass_to_development_and_skips_env_file():
    services = _override()["services"]
    for name in ("api", "worker", "beat"):
        svc = services[name]
        env = svc["environment"]
        assert env["AUTH_DEV_BYPASS"] == "true"
        # The API refuses to start with the bypass in production.
        assert env["ENVIRONMENT"] == "development"
        # Never read a developer's backend/.env (it may hold real secrets).
        assert svc["env_file"] == {"__tag__": "!reset", "value": []}
        for secret in (
            "GITHUB_TOKEN",
            "GITHUB_APP_ID",
            "GITHUB_APP_PRIVATE_KEY",
            "GROQ_API_KEY",
        ):
            assert env[secret] == "", f"{name} must blank {secret}"
        assert env["SUPABASE_URL"] == "http://fake-supabase:54321"
        assert svc["depends_on"]["fake-supabase"]["condition"] == "service_healthy"


def test_override_seed_matches_dev_bypass_org_and_script_repo():
    repos = _seed_from_override()["repositories"]
    assert repos[0]["org_id"] == DEV_ORG_ID
    assert repos[0]["full_name"] == SMOKE_REPO
    assert f'REPO="{SMOKE_REPO}"' in SCRIPT.read_text(encoding="utf-8")


def test_base_compose_still_defaults_to_production():
    base = yaml.safe_load(
        (REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    )
    for svc in base["services"].values():
        env = svc.get("environment") or {}
        assert "AUTH_DEV_BYPASS" not in env
        assert "fake-supabase" not in (svc.get("depends_on") or {})
    assert "fake-supabase" not in base["services"]


def test_script_is_strict_and_always_tears_down():
    text = SCRIPT.read_text(encoding="utf-8")
    assert "\r\n" not in text, "the script must keep LF line endings"
    assert text.startswith("#!/usr/bin/env bash\n")
    assert "set -euo pipefail" in text
    assert "trap cleanup EXIT" in text
    cleanup = text[text.index("cleanup() {") : text.index("trap cleanup EXIT")]
    assert "logs" in cleanup
    assert "down --volumes --remove-orphans" in cleanup
    assert "scripts/compose-smoke/compose.smoke.yml" in text
    assert "GitHub credentials unavailable" in text


def test_workflow_is_pinned_read_only_and_bounded():
    wf = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    # PyYAML parses the bare `on:` key as boolean True.
    triggers = wf.get("on", wf.get(True))
    paths = triggers["pull_request"]["paths"]
    for required in ("Dockerfile", "docker-compose.yml", "backend/**"):
        assert required in paths
    assert wf["permissions"] == {"contents": "read"}

    job = wf["jobs"]["compose-smoke"]
    assert job["permissions"] == {"contents": "read"}
    assert isinstance(job["timeout-minutes"], int) and job["timeout-minutes"] <= 60
    for step in job["steps"]:
        if "uses" in step:
            assert re.fullmatch(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}", step["uses"]), step[
                "uses"
            ]
    assert any(
        "scripts/compose-smoke.sh" in step.get("run", "") for step in job["steps"]
    )


def test_workflow_reuses_action_shas_from_existing_workflows():
    existing = set()
    for wf in (REPO_ROOT / ".github" / "workflows").glob("*.yml"):
        if wf.name != WORKFLOW.name:
            existing |= set(
                re.findall(
                    r"uses:\s*(\S+@[0-9a-f]{40})", wf.read_text(encoding="utf-8")
                )
            )
    ours = set(
        re.findall(r"uses:\s*(\S+@[0-9a-f]{40})", WORKFLOW.read_text(encoding="utf-8"))
    )
    assert ours and ours <= existing
