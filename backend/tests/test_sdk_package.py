"""The automaintainer SDK package surface and its packaging layout (#187).

- ``import automaintainer`` is free: no I/O and none of the runtime modules.
- The public names resolve lazily and ``__all__``/``dir()`` agree.
- The runtime modules live under the package; the old top-level names
  (``agents``, ``runners.local``, ...) are aliases of the same module objects,
  so callers and ``mock.patch`` targets on the old paths reach the code that
  runs.
- No module in the package imports an old top-level name: the wheel does not
  ship them.
"""

import ast
import fnmatch
import importlib
import json
import logging
import os
import pkgutil
import re
import subprocess
import sys
import tomllib
from pathlib import Path
from unittest import mock

import pytest

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

import automaintainer  # noqa: E402

PACKAGE_DIR = BACKEND / "automaintainer"

# The modules that moved into automaintainer/_runtime, by their old names.
RUNTIME_MODULES = (
    "agents",
    "ast_indexer",
    "change_limits",
    "events",
    "github_app",
    "merge_audit",
    "merge_policy",
    "rate_limiter",
    "redis_locks",
    "retrieval",
    "trust",
    "verification",
    "workspace",
)
RUNNER_SUBMODULES = ("base", "docker_sandbox", "local", "sandbox")
# Never loaded by the SDK: the server process and its dependencies.
SERVER_MODULES = ("main", "tasks", "celery_app", "quotas", "observability")
SERVER_PACKAGES = ("fastapi", "uvicorn", "celery")

PUBLIC = {
    "AutoMaintainerConfig",
    "AutoMaintainerEngine",
    "RunResult",
    "RunAlreadyActive",
    "cancel_active_run",
    "BaseRunner",
    "LocalRunner",
    "SandboxRunner",
    "SandboxBackend",
    "RunnerError",
    "PathTraversalError",
    "CommandTimeoutError",
    "WorkspaceError",
}


def _probe(code: str) -> dict:
    """Run code in a fresh interpreter (cwd=backend) and return its JSON."""
    env = {
        k: v
        for k, v in os.environ.items()
        if k in ("PATH", "SYSTEMROOT", "PYTHONHASHSEED")
    }
    result = subprocess.run(
        [sys.executable, "-B", "-c", code],
        cwd=str(BACKEND),
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


_LOADED = """
def loaded(prefixes):
    return sorted(
        m for m in sys.modules
        if any(m == p or m.startswith(p + ".") for p in prefixes)
    )
"""


def test_import_alone_does_no_io_and_loads_no_runtime_module():
    report = _probe(
        "import json, os, sys\n"
        + _LOADED
        + """
events = []
WRITE = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC

def hook(event, args):
    if event == "open":
        path, mode, flags = args
        if (mode and any(c in str(mode) for c in "wax+")) or (flags & WRITE):
            events.append([event, str(path)])
    elif event.startswith(("socket.", "subprocess.", "os.")) or event in (
        "shutil.rmtree", "tempfile.mkstemp", "tempfile.mkdtemp",
    ):
        if event not in ("os.listdir", "os.scandir", "os.chdir"):
            events.append([event, repr(args)[:200]])

before = dict(os.environ)
sys.addaudithook(hook)
import automaintainer
print(json.dumps({
    "events": events,
    "env_changed": dict(os.environ) != before,
    "loaded": loaded(%r),
    "version": automaintainer.__version__,
}))
"""
        % [
            "automaintainer._runtime",
            "automaintainer.runners",
            "automaintainer.engine",
            "automaintainer.context",
            "langgraph",
            "supabase",
            "github",
            "pydantic",
            "opentelemetry",
            *RUNTIME_MODULES,
            "runners",
            *SERVER_MODULES,
            *SERVER_PACKAGES,
        ]
    )

    assert report["events"] == []
    assert report["env_changed"] is False
    assert report["loaded"] == []
    assert report["version"] == automaintainer.__version__


def test_public_names_load_no_server_module_and_no_top_level_alias():
    """The acceptance check of #187, run from the source tree.

    The engine itself loads the runtime under automaintainer._runtime, never
    under an old top-level name (which the wheel does not ship), and none of
    the server modules. (opentelemetry is not checked: langsmith imports it
    whenever it is installed, as it is with the server extra.)
    """
    report = _probe("import json, sys\n" + _LOADED + """
import automaintainer
from automaintainer import AutoMaintainerEngine, LocalRunner, SandboxRunner
import automaintainer.runners
for name in automaintainer.__all__:
    getattr(automaintainer, name)
print(json.dumps({
    "server": loaded(%r),
    "top_level": loaded(%r),
    "runtime": "automaintainer._runtime.agents" in sys.modules,
}))
""" % ([*SERVER_MODULES, *SERVER_PACKAGES], [*RUNTIME_MODULES, "runners"]))

    assert report["server"] == []
    assert report["top_level"] == []
    assert report["runtime"] is True


def test_all_and_dir_list_the_same_public_names():
    assert set(automaintainer.__all__) == PUBLIC | {"__version__"}
    assert len(automaintainer.__all__) == len(set(automaintainer.__all__))
    assert set(automaintainer.__all__) <= set(dir(automaintainer))
    for name in automaintainer.__all__:
        assert getattr(automaintainer, name) is not None, name


def test_unknown_attribute_raises_attribute_error():
    with pytest.raises(AttributeError, match="no attribute 'nope'"):
        automaintainer.nope  # noqa: B018
    assert not hasattr(automaintainer, "agents")


def test_public_names_are_the_defining_objects():
    from automaintainer import config, engine, runners

    assert automaintainer.AutoMaintainerConfig is config.AutoMaintainerConfig
    assert automaintainer.AutoMaintainerEngine is engine.AutoMaintainerEngine
    assert automaintainer.cancel_active_run is engine.cancel_active_run
    assert automaintainer.LocalRunner is runners.LocalRunner
    assert issubclass(automaintainer.PathTraversalError, automaintainer.RunnerError)
    assert issubclass(automaintainer.LocalRunner, automaintainer.BaseRunner)
    assert set(runners.__all__) <= PUBLIC


def test_version_is_the_single_source_read_by_the_build_backend():
    pyproject = tomllib.loads((BACKEND / "pyproject.toml").read_text("utf-8"))
    assert "version" in pyproject["project"]["dynamic"]
    assert "version" not in pyproject["project"]
    path = pyproject["tool"]["hatch"]["version"]["path"]
    assert (BACKEND / path).resolve() == (PACKAGE_DIR / "__init__.py").resolve()
    assert re.fullmatch(r"\d+\.\d+\.\d+", automaintainer.__version__)


def test_wheel_ships_only_the_package():
    pyproject = tomllib.loads((BACKEND / "pyproject.toml").read_text("utf-8"))
    assert pyproject["build-system"]["build-backend"] == "hatchling.build"
    wheel = pyproject["tool"]["hatch"]["build"]["targets"]["wheel"]
    assert wheel["packages"] == ["automaintainer"]
    server = pyproject["project"]["optional-dependencies"]["server"]
    base = " ".join(pyproject["project"]["dependencies"])
    for dist in ("uvicorn", "celery", "opentelemetry"):
        assert dist not in base, dist
        assert any(dep.startswith(dist) for dep in server), dist


@pytest.mark.parametrize("name", RUNTIME_MODULES)
def test_old_module_name_is_the_moved_module(name):
    old = importlib.import_module(name)
    assert old is importlib.import_module(f"automaintainer._runtime.{name}")
    assert sys.modules[name] is old


@pytest.mark.parametrize("name", RUNNER_SUBMODULES)
def test_old_runner_module_name_is_the_moved_module(name):
    old = importlib.import_module(f"runners.{name}")
    assert old is importlib.import_module(f"automaintainer.runners.{name}")
    assert importlib.import_module("runners") is automaintainer.runners


def test_every_subpackage_is_a_regular_package():
    """No implicit namespace packages: tooling (mypy, coverage, pkgutil)
    treats those differently."""
    for directory in PACKAGE_DIR.rglob("*"):
        if directory.is_dir() and any(directory.glob("*.py")):
            assert (directory / "__init__.py").is_file(), directory
    import automaintainer._runtime as runtime

    assert runtime.__file__ is not None
    found = {m.name for m in pkgutil.iter_modules(runtime.__path__)}
    assert found == set(RUNTIME_MODULES)


def test_every_runner_submodule_is_aliased():
    """An unaliased submodule would load a second copy as runners.<name>."""
    import automaintainer.runners as package

    found = {m.name for m in pkgutil.iter_modules(package.__path__)}
    assert found == set(RUNNER_SUBMODULES)


def test_patching_the_old_path_reaches_the_code_that_runs():
    import automaintainer._runtime.change_limits as moved
    import automaintainer.runners.local as moved_local

    sentinel = object()
    with mock.patch("change_limits.DEFAULT_MAX_FILES", sentinel):
        assert moved.DEFAULT_MAX_FILES is sentinel
    with mock.patch("runners.local._to_argv") as fake:
        assert moved_local._to_argv is fake


@pytest.mark.parametrize(
    "spec",
    ["runners.docker_sandbox:create", "automaintainer.runners.docker_sandbox:create"],
)
def test_sandbox_backend_setting_loads_the_moved_module(spec, monkeypatch):
    """The documented old setting keeps working; SDK users use the new path."""
    from automaintainer.runners.sandbox import load_sandbox_backend_factory

    import automaintainer.runners.docker_sandbox as moved

    monkeypatch.setenv("SANDBOX_BACKEND", spec)
    assert load_sandbox_backend_factory() is moved.create


@pytest.mark.parametrize("name", RUNTIME_MODULES)
def test_logger_names_are_unchanged(name):
    module = importlib.import_module(name)
    logger = getattr(module, "logger", None)
    if isinstance(logger, logging.Logger):
        assert logger.name == name


def _absolute_imports(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield node.lineno, alias.name
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            yield node.lineno, node.module


def test_no_package_module_imports_an_old_top_level_name():
    """Lazy imports included: the wheel has no top-level agents, runners..."""
    banned = set(RUNTIME_MODULES) | {"runners", *SERVER_MODULES}
    offenders = [
        f"{path.relative_to(BACKEND)}:{line}: {name}"
        for path in sorted(PACKAGE_DIR.rglob("*.py"))
        for line, name in _absolute_imports(path)
        if name.split(".")[0] in banned
    ]
    assert offenders == []


REPO_ROOT = BACKEND.parent


def _install_commands():
    files = sorted((REPO_ROOT / ".github" / "workflows").glob("*.yml*"))
    files += [
        REPO_ROOT / "Dockerfile",
        REPO_ROOT / "scripts" / "export-backend-requirements.sh",
    ]
    for path in files:
        for lineno, line in enumerate(path.read_text("utf-8").splitlines(), 1):
            if re.search(r"\buv (sync|export)\b|args=\(export\b", line):
                yield f"{path.name}:{lineno}", line


def test_every_server_install_includes_the_server_extra():
    """The server deps moved to an extra; an install without it breaks main."""
    commands = list(_install_commands())
    assert len(commands) >= 8
    missing = [
        f"{where}: {line.strip()}"
        for where, line in commands
        if "--extra server" not in line and "--all-extras" not in line
    ]
    assert missing == []


def _moved_path(path: str):
    """The post-#187 path of a workflow path filter naming a moved file."""
    for name in RUNTIME_MODULES:
        if path == f"backend/{name}.py":
            return f"backend/automaintainer/_runtime/{name}.py"
    if path.startswith("backend/runners/"):
        return "backend/automaintainer/" + path[len("backend/") :]
    return None


def _path_filters():
    import yaml

    for wf in sorted((REPO_ROOT / ".github" / "workflows").glob("*.yml*")):
        doc = yaml.safe_load(wf.read_text("utf-8")) or {}
        # PyYAML reads the bare key ``on`` as True.
        triggers = doc.get("on", doc.get(True)) or {}
        if not isinstance(triggers, dict):
            continue
        for event, spec in triggers.items():
            if isinstance(spec, dict) and spec.get("paths"):
                yield f"{wf.name}:{event}", spec["paths"]


def test_moved_path_maps_old_names_to_the_package():
    assert _moved_path("backend/agents.py") == (
        "backend/automaintainer/_runtime/agents.py"
    )
    assert _moved_path("backend/runners/**") == "backend/automaintainer/runners/**"
    assert _moved_path("backend/evals/**") is None
    assert _moved_path("backend/**") is None


def test_every_workflow_path_filter_on_a_moved_file_names_its_new_path():
    """The old paths are alias files now: a filter naming only them would
    stop the workflow (e.g. the real-Docker sandbox job) running on changes
    to the code it guards."""
    filters = list(_path_filters())
    assert filters
    missing = []
    for where, paths in filters:
        for path in paths:
            new = _moved_path(path)
            if new is None:
                continue
            if not any(new == p or fnmatch.fnmatchcase(new, p) for p in paths):
                missing.append(f"{where}: {path} -> {new}")
    assert missing == []
