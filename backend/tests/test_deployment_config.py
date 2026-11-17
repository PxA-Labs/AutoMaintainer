"""Static regression tests for the deployment configuration (#396, #64, epic #418).

Docker is not available in CI test runs, so these tests parse the Dockerfile,
docker-compose.yml and render.yaml and assert the properties that matter for
correctness and security:

- the runtime image runs as a non-root user with ENVIRONMENT=production, a
  pinned gitnexus, no compilers and no `curl | bash`;
- the API and the Celery worker share the repository workspace directory
  (compose: same named volume at the same path; Render: same container);
- dangerous flags (ENABLE_TERMINAL, AUTH_DEV_BYPASS) are never set by default;
- the Render API service declares every GitHub App install-flow setting, with
  the secrets left to the dashboard (sync: false) (#479).
"""

import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKSPACE_DIR = "/var/lib/automaintainer/workspaces"


def _dockerfile_text() -> str:
    # Join backslash continuations so each instruction is one logical line.
    raw = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
    return re.sub(r"\\\r?\n", " ", raw)


def _stages() -> dict:
    """Map stage name -> list of instruction lines (comments stripped)."""
    stages, current = {}, None
    for line in _dockerfile_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"FROM\s+\S+(?:\s+AS\s+(\S+))?", line, re.IGNORECASE)
        if m:
            current = m.group(1) or f"stage{len(stages)}"
            stages[current] = []
        if current is not None:
            stages[current].append(line)
    return stages


def _final_stage() -> list:
    return list(_stages().values())[-1]


def _final_env(lines: list) -> dict:
    env = {}
    for line in lines:
        if line.upper().startswith("ENV "):
            for key, value in re.findall(r"(\w+)=(\"[^\"]*\"|\S+)", line[4:]):
                env[key] = value.strip('"')
    return env


def _compose() -> dict:
    return yaml.safe_load(
        (REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    )


def _render() -> dict:
    return yaml.safe_load((REPO_ROOT / "render.yaml").read_text(encoding="utf-8"))


# --- Dockerfile ---------------------------------------------------------------


def test_dockerfile_is_multi_stage():
    assert len(_stages()) >= 3


def test_runtime_runs_as_non_root_user():
    users = [ln.split()[1] for ln in _final_stage() if ln.upper().startswith("USER ")]
    assert users, "final stage must switch to an unprivileged USER"
    assert users[-1] not in {"root", "0", "0:0"}


def test_runtime_defaults_to_production():
    # Without this, production-only gates (e.g. the terminal) stay open (#396).
    env = _final_env(_final_stage())
    assert env.get("ENVIRONMENT") == "production"
    assert "ENABLE_TERMINAL" not in env
    assert "AUTH_DEV_BYPASS" not in env


def test_runtime_workspace_dir_matches_compose_volume():
    env = _final_env(_final_stage())
    assert env.get("AUTOMAINTAINER_WORKSPACE_DIR") == WORKSPACE_DIR


def test_gitnexus_is_pinned_and_node_not_installed_via_curl_pipe():
    text = "\n".join(" ".join(lines) for lines in _stages().values())  # no comments
    assert "@latest" not in text
    assert not re.search(r"curl[^|\n]*\|\s*(ba)?sh", text), "no curl | bash installers"
    m = re.search(r"ARG GITNEXUS_VERSION=(\S+)", text)
    assert m and re.fullmatch(
        r"\d+\.\d+\.\d+", m.group(1)
    ), "gitnexus must be an exact version"
    assert "gitnexus@${GITNEXUS_VERSION}" in text


def test_base_images_are_pinned_to_exact_versions():
    text = _dockerfile_text()
    for name in ("NODE_IMAGE", "PYTHON_IMAGE"):
        m = re.search(rf"ARG {name}=\S+:(\S+)", text)
        assert m, name
        assert re.match(
            r"\d+\.\d+\.\d+-", m.group(1)
        ), f"{name} must pin a full version"
    # gitnexus 1.6.x requires Node ^22.18 || >=24.11; Node 20 silently broke it.
    node = re.search(r"ARG NODE_IMAGE=node:(\d+)\.(\d+)", text)
    major, minor = int(node.group(1)), int(node.group(2))
    assert (major == 22 and minor >= 18) or major >= 24


def test_final_stage_has_no_build_toolchain():
    final = " ".join(_final_stage())
    for tool in ("build-essential", "g++", "gcc ", "curl"):
        assert tool not in final, f"{tool} must not ship in the runtime image"


def test_healthcheck_targets_existing_liveness_route():
    hc = [ln for ln in _final_stage() if ln.upper().startswith("HEALTHCHECK")]
    assert hc and "/healthz'" in hc[0]
    main_py = (REPO_ROOT / "backend" / "main.py").read_text(encoding="utf-8")
    assert '@app.get("/healthz")' in main_py


def test_cmd_honours_platform_port():
    cmd = [ln for ln in _final_stage() if ln.upper().startswith("CMD")][-1]
    assert "${PORT" in cmd


# --- docker-compose.yml -------------------------------------------------------


def test_compose_has_full_topology():
    services = _compose()["services"]
    assert {"api", "worker", "beat", "redis"} <= set(services)
    assert "worker" in services["worker"]["command"]
    assert "beat" in services["beat"]["command"]


@pytest.mark.parametrize("service", ["api", "worker"])
def test_compose_api_and_worker_share_workspace_volume(service):
    svc = _compose()["services"][service]
    assert f"workspaces:{WORKSPACE_DIR}" in svc["volumes"]
    assert svc["environment"]["AUTOMAINTAINER_WORKSPACE_DIR"] == WORKSPACE_DIR
    assert "redis" in svc["environment"]["REDIS_URL"]


def test_compose_worker_consumes_every_routed_queue():
    celery_app = (REPO_ROOT / "backend" / "celery_app.py").read_text(encoding="utf-8")
    routed = set(re.findall(r'\{"queue": "(\w+)"\}', celery_app))
    assert routed, "expected task_routes in celery_app.py"
    cmd = _compose()["services"]["worker"]["command"]
    queues = set(cmd[cmd.index("-Q") + 1].split(","))
    assert routed <= queues


def test_compose_never_enables_dangerous_flags():
    text = (REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert "AUTH_DEV_BYPASS" not in text
    assert "ENABLE_TERMINAL" not in text


# --- render.yaml --------------------------------------------------------------


def _render_app_services():
    return [s for s in _render()["services"] if s["type"] in {"web", "worker"}]


def test_render_sets_production_and_omits_dangerous_flags():
    for svc in _render_app_services():
        env = {e["key"]: e for e in svc.get("envVars", [])}
        assert env["ENVIRONMENT"].get("value") == "production", svc["name"]
        assert "ENABLE_TERMINAL" not in env, svc["name"]
        assert "AUTH_DEV_BYPASS" not in env, svc["name"]


def test_render_agent_worker_runs_with_api_in_single_instance():
    # Render disks cannot be shared between services, so the agent_runs worker
    # must run inside the web service (one instance) for IDE routes to see the
    # clones it creates (#396).
    services = _render_app_services()
    web = [s for s in services if s["type"] == "web"]
    assert len(web) == 1
    web = web[0]
    assert web.get("numInstances") == 1 and "scaling" not in web
    assert web["dockerCommand"] == "automaintainer-api-and-worker"
    assert "automaintainer-api-and-worker" in _dockerfile_text()
    web_env = {e["key"]: e.get("value") for e in web["envVars"]}
    assert "agent_runs" in web_env.get("CELERY_QUEUES", "agent_runs")
    # No other service may consume agent_runs: it would clone into its own,
    # unshared filesystem.
    for svc in services:
        if svc is not web:
            assert "agent_runs" not in svc.get("dockerCommand", "") + svc.get(
                "startCommand", ""
            )


# Settings the GitHub App install flow reads (#479): App authentication
# (GITHUB_APP_ID, GITHUB_APP_PRIVATE_KEY) and the install URL and setup
# callback (GITHUB_APP_SLUG, GITHUB_APP_CLIENT_ID, GITHUB_APP_CLIENT_SECRET)
# in github_app.py, and the setup redirect target (DASHBOARD_URL) in main.py.
# The list lives here because the backend has no single list of them;
# test_github_app_install_settings_are_read_by_the_backend keeps it honest.
GITHUB_APP_INSTALL_SETTINGS = (
    "GITHUB_APP_ID",
    "GITHUB_APP_PRIVATE_KEY",
    "GITHUB_APP_SLUG",
    "GITHUB_APP_CLIENT_ID",
    "GITHUB_APP_CLIENT_SECRET",
    "DASHBOARD_URL",
)
GITHUB_APP_SECRETS = ("GITHUB_APP_CLIENT_SECRET", "GITHUB_APP_PRIVATE_KEY")


def _render_web(render: dict) -> dict:
    (web,) = [s for s in render["services"] if s["type"] == "web"]
    return web


def _github_app_settings_problems(render: dict) -> list:
    """Install-flow settings missing from the Render API service, and secrets
    that are not ``sync: false`` or that carry a value in the blueprint."""
    env = {e["key"]: e for e in _render_web(render).get("envVars", [])}
    problems = [
        f"{key} is not declared"
        for key in GITHUB_APP_INSTALL_SETTINGS
        if key not in env
    ]
    for key in GITHUB_APP_SECRETS:
        entry = env.get(key)
        if entry is None:
            continue
        if entry.get("sync") is not False:
            problems.append(f"{key} must use sync: false")
        if {"value", "generateValue", "fromService", "fromGroup"} & set(entry):
            problems.append(f"{key} must not have a value")
    return problems


def test_render_api_declares_every_github_app_install_setting():
    assert _github_app_settings_problems(_render()) == []


def test_github_app_install_settings_are_read_by_the_backend():
    """A setting dropped or renamed in the backend must leave this list too."""
    sources = "".join(
        (REPO_ROOT / "backend" / name).read_text(encoding="utf-8")
        for name in ("automaintainer/_runtime/github_app.py", "main.py")
    )
    for key in GITHUB_APP_INSTALL_SETTINGS:
        assert f'os.getenv("{key}"' in sources, key


@pytest.mark.parametrize("key", GITHUB_APP_INSTALL_SETTINGS)
def test_render_check_fails_when_a_setting_is_removed(key):
    render = _render()
    web = _render_web(render)
    web["envVars"] = [e for e in web["envVars"] if e["key"] != key]
    assert _github_app_settings_problems(render) == [f"{key} is not declared"]


@pytest.mark.parametrize("key", GITHUB_APP_SECRETS)
@pytest.mark.parametrize(
    "entry, expected",
    [
        ({"value": "s3cret"}, ["must use sync: false", "must not have a value"]),
        ({"sync": False, "value": "s3cret"}, ["must not have a value"]),
        ({"sync": True}, ["must use sync: false"]),
    ],
)
def test_render_check_fails_for_a_secret_with_a_literal_value(key, entry, expected):
    render = _render()
    web = _render_web(render)
    web["envVars"] = [
        {"key": key, **entry} if e["key"] == key else e for e in web["envVars"]
    ]
    assert _github_app_settings_problems(render) == [f"{key} {m}" for m in expected]
