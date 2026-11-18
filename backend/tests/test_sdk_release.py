"""Packaging and release of the automaintainer SDK (#524).

- The distribution is named after its import package, ``automaintainer``.
- Local build output (``uv build`` writes backend/dist) never enters the
  Docker build context.
- ``.github/workflows/publish-sdk.yml`` builds on an ``sdk-v<version>`` tag,
  checks the tag against ``automaintainer.__version__``, publishes to
  TestPyPI first and to PyPI only after that, from a protected environment
  (the manual approval), with OIDC trusted publishing (no API token), and
  publishes nothing until the owner turns it on.
"""

import re
import tomllib
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND = REPO_ROOT / "backend"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "publish-sdk.yml"
PUBLISH_ACTION = "pypa/gh-action-pypi-publish@"


def _workflow():
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _steps_using(job, action):
    return [s for s in job["steps"] if s.get("uses", "").startswith(action)]


def test_distribution_is_named_after_the_import_package():
    project = tomllib.loads((BACKEND / "pyproject.toml").read_text("utf-8"))
    assert project["project"]["name"] == "automaintainer"
    wheel = project["tool"]["hatch"]["build"]["targets"]["wheel"]
    assert wheel["packages"] == ["automaintainer"]


def test_build_output_is_kept_out_of_the_docker_context():
    lines = (REPO_ROOT / ".dockerignore").read_text("utf-8").splitlines()
    for pattern in ("backend/dist", "backend/build", "**/*.egg-info"):
        assert pattern in lines, pattern


def test_publish_runs_on_sdk_tags_only():
    # PyYAML reads the bare key `on` as True.
    triggers = _workflow()[True]
    assert set(triggers) == {"push"}
    assert triggers["push"] == {"tags": ["sdk-v*.*.*"]}
    # release.yml's v*.*.* tags never match, and sdk tags never match it.
    assert not re.fullmatch(r"v.*\..*\..*", "sdk-v0.1.0")


def test_testpypi_first_then_pypi_behind_an_environment():
    jobs = _workflow()["jobs"]
    test, prod = jobs["publish-testpypi"], jobs["publish-pypi"]

    assert test["needs"] == "build"
    assert prod["needs"] == "publish-testpypi"
    assert test["environment"]["name"] == "testpypi"
    assert prod["environment"]["name"] == "pypi"
    (test_step,) = _steps_using(test, PUBLISH_ACTION)
    (prod_step,) = _steps_using(prod, PUBLISH_ACTION)
    assert test_step["with"]["repository-url"] == "https://test.pypi.org/legacy/"
    assert "repository-url" not in prod_step.get("with", {})


def test_trusted_publishing_only_in_the_publish_jobs_and_no_token():
    jobs = _workflow()["jobs"]
    for name, job in jobs.items():
        perms = job.get("permissions", {})
        if name.startswith("publish-"):
            assert perms == {"id-token": "write"}, name
            # Inert until the owner enables it (docs/DEPLOYMENT.md).
            assert "vars.SDK_PUBLISH_ENABLED == 'true'" in job["if"], name
        else:
            assert "id-token" not in perms, name
    text = WORKFLOW.read_text("utf-8")
    assert "password:" not in text and "PYPI_API_TOKEN" not in text


def test_build_checks_the_tag_against_the_package_version():
    build = _workflow()["jobs"]["build"]
    commands = "\n".join(s.get("run", "") for s in build["steps"])
    assert "uv build" in commands
    assert "__version__" in commands
    # The tag reaches the script through the environment, never inlined.
    assert "${{" not in commands
