"""Celery beat schedule tests (need real Celery; skipped with the fallback shim)."""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def test_sync_repositories_runs_once_every_six_hours():
    pytest.importorskip("celery")
    from celery_app import celery_app

    schedule = celery_app.conf.beat_schedule["sync-repositories"]["schedule"]
    # crontab(hour="*/6") alone defaults minute to "*": 60 firings per hour.
    assert schedule.minute == {0}
    assert schedule.hour == {0, 6, 12, 18}
