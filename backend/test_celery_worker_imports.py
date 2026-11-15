"""The worker must load its task modules the way the image starts it.

The image runs `celery -A celery_app worker` from /app/backend, so only the
backend directory is importable and there is no `backend` package. An
`autodiscover_tasks(["backend"])` call made every worker crash at startup
with ModuleNotFoundError (found by the compose smoke test, #396).
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("celery")

BACKEND = Path(__file__).resolve().parent

PROBE = """
from celery_app import celery_app
celery_app.loader.import_default_modules()
names = sorted(n for n in celery_app.tasks if not n.startswith("celery."))
print(" ".join(names))
"""


def test_worker_imports_task_modules_from_the_backend_directory():
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env["PYTHONPATH"] = str(BACKEND)
    env.setdefault("REDIS_URL", "redis://127.0.0.1:6399/0")
    proc = subprocess.run(
        [sys.executable, "-c", PROBE],
        cwd=BACKEND,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr[-3000:]
    registered = set(proc.stdout.split())
    assert {
        "tasks.run_agent_loop_task",
        "tasks.cleanup_stale_runs",
        "tasks.sync_repositories",
        "tasks.cancel_run_task",
    } <= registered
