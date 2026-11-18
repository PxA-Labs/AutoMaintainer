"""Static checks on the backend CI coverage and lint pins (#532).

- black and flake8 are exact pins in the uv ``dev`` group, locked in uv.lock,
  and the lint job runs them through uv from the lock, so local and CI runs
  use the same versions. The flake8 gate selection stays as it was.
- A CI job installs the ``adk`` and ``rag`` extras and runs the tests that
  otherwise skip (specialists, the ADK reviewer concurrency test, rag and the
  red-team fixtures), and fails if any of them still skips.
- README names the wheel the build produces since #529.
"""

import re
import tomllib
from pathlib import Path

import yaml

BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_DIR.parent
CI_BACKEND = REPO_ROOT / ".github" / "workflows" / "ci-backend.yml"

PIN_RE = re.compile(r"^(?P<name>[A-Za-z0-9_.-]+)==(?P<version>[0-9][\w.]*)$")
LINTERS = ("black", "flake8")


def _pyproject():
    return tomllib.loads((BACKEND_DIR / "pyproject.toml").read_text("utf-8"))


def _dev_pins():
    pins = {}
    for req in _pyproject()["dependency-groups"]["dev"]:
        m = PIN_RE.match(req)
        if m:
            pins[m.group("name").lower()] = m.group("version")
    return pins


def _jobs():
    return yaml.safe_load(CI_BACKEND.read_text("utf-8"))["jobs"]


def _commands(job):
    return "\n".join(step.get("run", "") for step in job["steps"])


def test_dev_group_pins_black_and_flake8_exactly():
    pins = _dev_pins()
    for name in LINTERS:
        assert name in pins, f"{name} is not pinned with == in the dev group"


def test_linters_stay_out_of_the_base_install_and_extras():
    project = _pyproject()["project"]
    declared = list(project["dependencies"])
    for reqs in project.get("optional-dependencies", {}).values():
        declared += reqs
    names = {re.split(r"[<>=!~;\[ ]", r, maxsplit=1)[0].lower() for r in declared}
    assert not names & set(LINTERS)


def test_uv_lock_has_the_pinned_linter_versions():
    lock = tomllib.loads((BACKEND_DIR / "uv.lock").read_text("utf-8"))
    locked = {}
    for pkg in lock["package"]:
        # The project itself has a dynamic version, so no "version" key.
        locked.setdefault(pkg["name"], set()).add(pkg.get("version"))
    pins = _dev_pins()
    for name in LINTERS:
        assert name in pins, name
        assert locked.get(name) == {pins[name]}, (name, locked.get(name))


def test_lint_job_runs_the_locked_linters_through_uv():
    commands = _commands(_jobs()["lint-and-check"])
    # No unpinned pip install of either linter.
    assert not re.search(r"pip install[^\n]*\b(flake8|black)\b", commands)
    assert "uv sync --frozen --only-group dev" in commands
    assert "uv run --frozen --no-sync black --check ." in commands
    # The flake8 gate selection is unchanged.
    assert (
        "uv run --frozen --no-sync flake8 . --count --select=E9,F63,F7,F82 "
        "--show-source --statistics"
    ) in commands
    assert (
        "uv run --frozen --no-sync flake8 . --count --exit-zero --max-complexity=10 "
        "--max-line-length=127 --statistics"
    ) in commands


def test_lint_job_keeps_the_venv_out_of_the_linted_tree():
    # flake8 has no default exclude for .venv; a venv under backend/ would be
    # linted (and black would have to rely on its own default excludes).
    # The runner context is not allowed in job-level env (actionlint), so the
    # path is exported through GITHUB_ENV before the sync.
    commands = _commands(_jobs()["lint-and-check"])
    export = 'echo "UV_PROJECT_ENVIRONMENT=$RUNNER_TEMP/lint-venv" >> "$GITHUB_ENV"'
    assert export in commands
    assert commands.index(export) < commands.index("uv sync --frozen")


EXTRA_TEST_PATHS = (
    "tests/specialists",
    "tests/concurrency/test_concurrency_adk_reviewer.py",
    "tests/rag",
    "tests/redteam",
)


def _extras_job():
    matches = [
        job
        for job in _jobs().values()
        if "--extra adk" in _commands(job) and "--extra rag" in _commands(job)
    ]
    assert len(matches) == 1, "expected one CI job that installs adk and rag"
    return matches[0]


def test_a_ci_job_installs_the_adk_and_rag_extras_from_the_lock():
    commands = _commands(_extras_job())
    assert (
        "uv sync --frozen --extra server --extra adk --extra rag --group dev"
        in commands
    )


def test_the_extras_job_runs_the_otherwise_skipped_tests():
    steps = [s.get("run", "") for s in _extras_job()["steps"]]
    joined = " ".join(run for run in steps if "pytest" in run)
    for path in EXTRA_TEST_PATHS:
        assert path in joined, path
        assert (BACKEND_DIR / path).exists(), path


def test_the_extras_job_fails_when_a_test_skips():
    # ripgrep is installed because some specialist tool tests need it, and
    # the junit report is checked so a missing extra cannot pass as skips.
    commands = _commands(_extras_job())
    assert "ripgrep" in commands
    assert "--junitxml" in commands
    assert "skipped" in commands


def test_readme_names_the_automaintainer_wheel():
    readme = (REPO_ROOT / "README.md").read_text("utf-8")
    assert "automaintainer_backend" not in readme
    assert "automaintainer-<version>-py3-none-any.whl" in readme
