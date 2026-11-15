"""render.yaml declares the GitHub App install-flow variables (#470).

POST /github/app/install-url and GET /github/app/setup (main.py) read
GITHUB_APP_SLUG, GITHUB_APP_CLIENT_ID, GITHUB_APP_CLIENT_SECRET
(github_app.install_flow_configured / build_install_url / exchange_oauth_code)
and DASHBOARD_URL (main._dashboard_base_url). Only the web service runs the
API, so it is the one that must declare them; each is per-deployment, so it
is prompted for (sync: false) and never given a value in the blueprint.
"""

from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
INSTALL_FLOW_VARS = (
    "GITHUB_APP_SLUG",
    "GITHUB_APP_CLIENT_ID",
    "GITHUB_APP_CLIENT_SECRET",
    "DASHBOARD_URL",
)


def _web_env() -> dict:
    blueprint = yaml.safe_load((REPO_ROOT / "render.yaml").read_text("utf-8"))
    (web,) = [s for s in blueprint["services"] if s["type"] == "web"]
    return {e["key"]: e for e in web["envVars"]}


@pytest.mark.parametrize("key", INSTALL_FLOW_VARS)
def test_web_service_declares_install_flow_variable_unsynced(key):
    env = _web_env()
    assert key in env, f"render.yaml web service does not declare {key}"
    assert env[key].get("sync") is False
    assert "value" not in env[key]


def test_install_flow_variables_match_what_the_backend_reads():
    source = (REPO_ROOT / "backend" / "github_app.py").read_text("utf-8")
    source += (REPO_ROOT / "backend" / "main.py").read_text("utf-8")
    for key in INSTALL_FLOW_VARS:
        assert f'"{key}"' in source, f"{key} is no longer read by the backend"
