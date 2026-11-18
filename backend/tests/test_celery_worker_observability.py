"""The Celery worker sets up OpenTelemetry and its heartbeat (#527).

celery_app must call observability.install_celery_worker_hooks() at import:
each prefork child then runs init_worker_observability (providers, the
Cloud Monitoring reader when switched on, and the celery_worker_heartbeat
gauge the "worker heartbeat absent" alert watches) after the fork, and
flushes its metrics when it exits.

The import runs in a subprocess, the way the worker starts
(``celery -A celery_app worker`` from backend/), so the signal receivers of
this test session are not involved.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

import observability  # noqa: E402

PROBE = """
import observability
calls = []
observability.init_worker_observability = lambda: calls.append("init")
observability.shutdown_observability = lambda: calls.append("shutdown")
import celery_app
from celery.signals import worker_process_init, worker_process_shutdown
worker_process_init.send(sender=None)
worker_process_shutdown.send(sender=None, pid=1, exitcode=0)
print("hooks=" + ",".join(calls))
"""


def test_importing_celery_app_installs_the_worker_hooks():
    pytest.importorskip("celery")
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
    assert "hooks=init,shutdown" in proc.stdout.splitlines(), proc.stdout[-2000:]


def test_worker_init_registers_the_heartbeat(monkeypatch):
    calls = []
    monkeypatch.setattr(
        observability, "init_observability", lambda **k: calls.append(("init", k))
    )
    monkeypatch.setattr(
        observability, "register_worker_heartbeat", lambda: calls.append("heartbeat")
    )
    observability.init_worker_observability()
    assert calls[0][0] == "init" and calls[1] == "heartbeat"
    assert calls[0][1]["service_name"] == "automaintainer-worker"
    # A worker serves no /metrics endpoint.
    assert calls[0][1]["enable_prometheus"] is False
