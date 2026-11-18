"""``.env`` and the GitHub auth-mode warning belong to process startup (#524).

Importing the SDK (``automaintainer``, ``AutoMaintainerEngine``, the runtime
modules) must not load a ``.env`` file into the embedding process or print
anything: an application that embeds the engine owns its environment and
its log output. The API (``main``) and the Celery worker (``celery_app``)
still load ``.env`` and log the PAT-mode warning once, before any module
reads its settings from the environment.

Each case runs in a fresh interpreter (the module caches of this test
session already hold every module), from an empty temporary directory, with
the GitHub App and auth-mode variables removed so the PAT-mode warning would
fire. ``dotenv.load_dotenv`` is replaced before anything imports it: the
stand-in records each call (and which modules were already loaded) and sets
two variables the way a ``.env`` file would. An audit hook records every
file named ``.env`` that is opened.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parent

PROBE = r"""
import json
import logging
import os
import sys

sys.path.insert(0, sys.argv[1])
statement = sys.argv[2]

opened = []


def hook(event, args):
    if event == "open" and isinstance(args[0], (str, bytes, os.PathLike)):
        if os.path.basename(os.fsdecode(args[0])) == ".env":
            opened.append(os.fsdecode(args[0]))


sys.addaudithook(hook)

import dotenv  # noqa: E402

calls = []
WATCHED = ("automaintainer._runtime.agents", "automaintainer._runtime.redis_locks")


def fake_load_dotenv(*args, **kwargs):
    calls.append(sorted(m for m in WATCHED if m in sys.modules))
    os.environ.setdefault("REDIS_URL", "redis://from-dotenv:6379/0")
    os.environ.setdefault("GITHUB_WEBHOOK_SECRET", "secret-from-dotenv")
    return True


dotenv.load_dotenv = fake_load_dotenv

records = []
if os.environ.get("PROBE_CAPTURE_LOGS"):

    class Capture(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    logging.getLogger().addHandler(Capture())

exec(statement)

github_app = sys.modules.get("automaintainer._runtime.github_app")
celery_app = sys.modules.get("celery_app")
print(
    "PROBE-RESULT "
    + json.dumps(
        {
            "calls": calls,
            "opened": opened,
            "pat_warnings": sum("auth mode is 'pat'" in r for r in records),
            "webhook_secret": getattr(github_app, "GITHUB_WEBHOOK_SECRET", None),
            "celery_redis_url": getattr(celery_app, "REDIS_URL", None),
        }
    )
)
"""

CLEARED = (
    "GITHUB_AUTH_MODE",
    "GITHUB_APP_ID",
    "GITHUB_APP_PRIVATE_KEY",
    "GITHUB_APP_PRIVATE_KEY_PATH",
    "GITHUB_WEBHOOK_SECRET",
    "REDIS_URL",
)


def _probe(tmp_path, statement, *, capture_logs=False):
    env = {k: v for k, v in os.environ.items() if k not in CLEARED}
    env.pop("PROBE_CAPTURE_LOGS", None)
    if capture_logs:
        env["PROBE_CAPTURE_LOGS"] = "1"
    proc = subprocess.run(
        [sys.executable, "-W", "ignore", "-c", PROBE, str(BACKEND_DIR), statement],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert proc.returncode == 0, proc.stderr
    out_lines = proc.stdout.splitlines()
    assert out_lines and out_lines[-1].startswith("PROBE-RESULT "), proc.stdout
    result = json.loads(out_lines[-1][len("PROBE-RESULT ") :])
    result["stdout"] = out_lines[:-1]
    result["stderr"] = proc.stderr
    return result


SDK_IMPORTS = [
    "import automaintainer",
    "from automaintainer import AutoMaintainerEngine",
    "import automaintainer._runtime.github_app",
    "import automaintainer._runtime.agents",
    "import github_app, agents",  # the compatibility aliases
]


@pytest.mark.parametrize("statement", SDK_IMPORTS)
def test_importing_the_sdk_reads_no_env_file_and_prints_nothing(tmp_path, statement):
    result = _probe(tmp_path, statement)

    assert result["calls"] == []
    assert result["opened"] == []
    assert result["stdout"] == [] and result["stderr"] == ""


@pytest.mark.parametrize("module", ["main", "celery_app"])
def test_api_and_worker_startup_load_env_and_warn_once(tmp_path, module):
    result = _probe(tmp_path, f"import {module}", capture_logs=True)

    # Once, before the modules that read settings at import were loaded.
    assert result["calls"] == [[]]
    assert result["pat_warnings"] == 1
    # Values from .env reach the settings read at import.
    assert result["celery_redis_url"] == "redis://from-dotenv:6379/0"
    assert result["webhook_secret"] == "secret-from-dotenv"
