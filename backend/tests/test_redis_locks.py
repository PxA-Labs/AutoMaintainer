"""
Basic unit tests for redis_locks.py.

No real Redis server needed -- a tiny in-memory FakeRedis stands in for
the actual client, so this only tests our lock logic (NX-acquire,
owner-checked release, heartbeat), not Redis itself.

Run with: pytest tests/test_redis_locks.py -v
Needs only: pip install pytest
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import redis_locks


class FakeRedis:
    """Minimal in-memory stand-in for redis.Redis -- just what our code calls."""

    def __init__(self):
        self.store = {}

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.store:
            return None
        self.store[key] = value
        return True

    def eval(self, script, numkeys, key, value):
        # Mirrors the compare-and-delete Lua script's behavior.
        if self.store.get(key) == value:
            del self.store[key]
            return 1
        return 0

    def ping(self):
        return True

    def exists(self, key):
        return int(key in self.store)


def setup_function(_fn):
    # Force our fake in before each test instead of a real Redis connection.
    redis_locks._redis_client = FakeRedis()


def test_acquire_lock_succeeds_when_free():
    assert redis_locks.acquire_repo_lock("repo-1", "run-a") is True


def test_acquire_lock_fails_when_already_held():
    assert redis_locks.acquire_repo_lock("repo-1", "run-a") is True
    assert redis_locks.acquire_repo_lock("repo-1", "run-b") is False


def test_release_only_works_for_the_owning_run():
    redis_locks.acquire_repo_lock("repo-1", "run-a")

    # run-b never held this lock -- its release must be a no-op.
    redis_locks.release_repo_lock("repo-1", "run-b")
    assert redis_locks.acquire_repo_lock("repo-1", "run-c") is False

    # run-a is the real owner and can release it.
    redis_locks.release_repo_lock("repo-1", "run-a")
    assert redis_locks.acquire_repo_lock("repo-1", "run-c") is True


def test_heartbeat_write_and_check():
    redis_locks.write_heartbeat("run-a")
    assert redis_locks.is_heartbeat_alive("run-a") is True
    assert redis_locks.is_heartbeat_alive("run-does-not-exist") is False


def test_is_redis_available_false_without_env_var(monkeypatch):
    monkeypatch.delenv("REDIS_URL", raising=False)
    assert redis_locks.is_redis_available() is False


def test_is_redis_available_true_when_configured_and_reachable(monkeypatch):
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    # FakeRedis.ping() always succeeds, so this exercises the "reachable" path.
    assert redis_locks.is_redis_available() is True
