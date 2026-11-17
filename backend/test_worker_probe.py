"""The agent worker check lives in worker_probe, outside the API module (#518).

POST /start (main) and the issue-label trigger (webhook_processor) share one
probe. webhook_processor runs in the github_sync Celery worker, which must not
load main or FastAPI to use it.
"""

import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

import main
import webhook_processor
import worker_probe

BACKEND = Path(__file__).resolve().parent

# The github_sync worker: `celery -A celery_app worker` loads the include=
# task modules, then processes a label event, which runs the worker check.
WORKER = """
import sys
from celery_app import celery_app
celery_app.loader.import_default_modules()
import webhook_processor
assert webhook_processor._agent_worker_available() is True
loaded = sorted(
    m for m in sys.modules if m.split(".")[0] in {"main", "fastapi", "starlette"}
)
print("LOADED:" + ",".join(loaded))
"""


def test_github_sync_worker_does_not_import_main_or_fastapi():
    pytest.importorskip("celery")
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env["PYTHONPATH"] = str(BACKEND)
    env.setdefault("REDIS_URL", "redis://127.0.0.1:6399/0")
    # No broker in tests: the check is switched off, so the call above runs
    # worker_probe's code path without a network round trip.
    env["AGENT_WORKER_CHECK"] = "false"
    proc = subprocess.run(
        [sys.executable, "-c", WORKER],
        cwd=BACKEND,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr[-3000:]
    [loaded] = [line for line in proc.stdout.splitlines() if line.startswith("LOADED:")]
    assert loaded == "LOADED:", loaded


def test_main_uses_the_shared_check():
    assert main.agent_worker_available is worker_probe.agent_worker_available
    assert main.AGENT_WORKER_UNAVAILABLE == worker_probe.AGENT_WORKER_UNAVAILABLE


def test_label_trigger_follows_a_patch_of_the_shared_check(monkeypatch):
    check = AsyncMock(return_value=False)
    monkeypatch.setattr(worker_probe, "agent_worker_available", check)

    assert webhook_processor._agent_worker_available() is False
    check.assert_awaited_once()


def test_start_and_label_trigger_share_the_cached_answer(monkeypatch):
    """One cache per process: an answer /start's probe stored is the one the
    label path reads, and the reverse."""
    monkeypatch.delenv("AGENT_WORKER_CHECK", raising=False)
    probe = {"checked_at": time.monotonic(), "available": True, "future": None}
    monkeypatch.setattr(worker_probe, "_worker_probe", probe)
    monkeypatch.setattr(
        worker_probe,
        "_probe_agent_worker",
        lambda: pytest.fail("a fresh cached answer must not probe again"),
    )

    assert asyncio.run(main.agent_worker_available()) is True
    assert webhook_processor._agent_worker_available() is True
    probe["available"] = False
    assert asyncio.run(main.agent_worker_available()) is False
    assert webhook_processor._agent_worker_available() is False
