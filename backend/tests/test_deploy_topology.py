"""Static checks of the supported deployment topology (#396, #387).

Complements test_deployment_config.py: the API and every worker must reach
the same broker, the Compose stack must start in dependency order with
healthchecks, and secrets must come from an uncommitted env file. Parsing
only; Docker is not needed (scripts/compose-smoke.sh runs the real stack).
"""

import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
APP_SERVICES = ("api", "worker", "beat")


def _compose() -> dict:
    return yaml.safe_load(
        (REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    )


def _render() -> dict:
    return yaml.safe_load((REPO_ROOT / "render.yaml").read_text(encoding="utf-8"))


# --- Render -------------------------------------------------------------------


def test_render_api_and_workers_share_one_redis():
    services = _render()["services"]
    redis = [s for s in services if s["type"] in {"redis", "keyvalue"}]
    assert len(redis) == 1
    assert redis[0].get("maxmemoryPolicy") == "noeviction"

    app = [s for s in services if s["type"] in {"web", "worker"}]
    assert {s["type"] for s in app} == {"web", "worker"}
    for svc in app:
        env = {e["key"]: e for e in svc["envVars"]}
        source = env["REDIS_URL"].get("fromService")
        assert source == {
            "type": redis[0]["type"],
            "name": redis[0]["name"],
            # The only Key Value URL property in Render's Blueprint schema.
            "property": "connectionString",
        }, svc["name"]


# --- docker compose -----------------------------------------------------------


@pytest.mark.parametrize("service", APP_SERVICES)
def test_compose_app_services_wait_for_healthy_redis(service):
    svc = _compose()["services"][service]
    assert svc["depends_on"]["redis"]["condition"] == "service_healthy"
    assert svc["environment"]["REDIS_URL"].startswith("redis://redis:")


def test_compose_healthchecks():
    services = _compose()["services"]
    assert "redis-cli" in " ".join(services["redis"]["healthcheck"]["test"])
    worker_check = " ".join(services["worker"]["healthcheck"]["test"])
    assert "inspect ping" in worker_check
    # api uses the image HEALTHCHECK on /healthz, so it must not disable it.
    assert not services["api"].get("healthcheck", {}).get("disable")


def test_compose_only_publishes_the_api():
    services = _compose()["services"]
    assert services["api"]["ports"]
    for name, svc in services.items():
        if name != "api":
            assert not svc.get("ports"), name


def test_compose_runs_beat_because_celery_schedules_periodic_tasks():
    celery_app = (REPO_ROOT / "backend" / "celery_app.py").read_text(encoding="utf-8")
    assert re.search(r"beat_schedule\s*=\s*\{\s*\S", celery_app)
    assert "beat" in _compose()["services"]["beat"]["command"]


@pytest.mark.parametrize("service", APP_SERVICES)
def test_compose_reads_secrets_from_uncommitted_env_file(service):
    env_files = _compose()["services"][service]["env_file"]
    assert {"path": "backend/.env", "required": False} in env_files


def test_env_file_is_ignored_by_git_and_docker():
    gitignore = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert ".env" in [line.strip() for line in gitignore]
    dockerignore = (REPO_ROOT / ".dockerignore").read_text(encoding="utf-8")
    assert "**/.env" in dockerignore.splitlines()


def test_env_example_lists_required_names_without_values():
    lines = (
        (REPO_ROOT / "backend" / ".env.example")
        .read_text(encoding="utf-8")
        .splitlines()
    )
    assignments = dict(
        line.split("=", 1) for line in lines if line and not line.startswith("#")
    )
    for name in (
        "SUPABASE_URL",
        "SUPABASE_SERVICE_KEY",
        "GROQ_API_KEY",
        "ALLOWED_ORIGINS",
    ):
        assert name in assignments
    # A template only: no value may be committed.
    assert all(value == "" for value in assignments.values())
    # Compose sets these itself; listing them would suggest they are needed.
    for name in ("REDIS_URL", "AUTOMAINTAINER_WORKSPACE_DIR"):
        assert name not in assignments
