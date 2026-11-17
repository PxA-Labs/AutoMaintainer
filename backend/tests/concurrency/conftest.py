"""Shared setup of the concurrency and isolation suite (#187, epic #157).

Every test here starts from clean process-wide state (the installation token
cache, the per-root scrub set, the App slug cache) and runs with GitHub,
Supabase and Groq faked: GET /app answers with the slug of whichever App
signs the request, installation tokens are minted per (App, installation)
without a network call, and git is never run. ``mint_log`` records every
mint so a test can count them per cache key.
"""

import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

BACKEND = Path(__file__).resolve().parents[2]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import agents  # noqa: E402
import github_app  # noqa: E402
import workspace  # noqa: E402


class MintLog:
    """Thread-safe record of installation token mints, as (App id, installation)."""

    def __init__(self):
        self._lock = threading.Lock()
        self.calls = []

    def add(self, app_id, installation_id):
        with self._lock:
            self.calls.append((app_id, installation_id))

    def count(self, app_id, installation_id):
        with self._lock:
            return self.calls.count((app_id, installation_id))


@pytest.fixture
def mint_log():
    return MintLog()


@pytest.fixture(autouse=True)
def _fake_github_and_clean_caches(monkeypatch, mint_log):
    monkeypatch.setattr(workspace, "_scrubbed_roots", set())
    monkeypatch.setattr(agents, "_github_app_slug_cache", {})

    def integration():
        app_id = github_app.current_app_identity().app_id
        return SimpleNamespace(get_app=lambda: SimpleNamespace(slug=f"app-{app_id}"))

    monkeypatch.setattr(github_app, "get_github_integration", integration)

    def mint(installation_id, repository, required, optional, app=None):
        app_id = (app or github_app.current_app_identity()).app_id
        mint_log.add(app_id, installation_id)
        return f"tok-{app_id}-{installation_id}", time.time() + 3600

    monkeypatch.setattr(github_app, "_mint_installation_token", mint)

    async def no_git(*args, **kwargs):  # pragma: no cover - fails the test
        raise AssertionError(f"git {args[:1]} called in the concurrency suite")

    monkeypatch.setattr(workspace, "_run_git", no_git)
    github_app._installation_token_cache.clear()
    yield
    github_app._installation_token_cache.clear()
