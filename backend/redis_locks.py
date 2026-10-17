"""
Distributed locks and Redis-availability check for AutoMaintainer.

Why this exists:
- "Distributed state locks in Redis preventing duplicate simultaneous
  runs on the same repository."
- "Zero-overhead fallback when Redis is not configured for local
  single-process development."

Design notes:
- The lock is a Redis key set with NX (only-if-not-exists) and an
  expiry (TTL). NX gives us atomic "acquire only if free" semantics,
  and the TTL means a crashed worker can never hold the lock forever
  -- it's released automatically after LOCK_TTL_SECONDS even if
  release_repo_lock() never runs.
- The lock's *value* is the run_id that holds it. Releasing checks
  that we still own the lock before deleting it (via a small Lua
  script, so the check-then-delete is atomic). Without this, a run
  that overstays its TTL could have its lock reassigned to a new run,
  and the old run's (delayed) cleanup could then delete the new run's
  lock out from under it.
"""

import os
import redis

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

# Matches the agent task's 30-minute soft time limit in celery_app.py,
# so a stuck task can't hold the repo lock past its own timeout.
LOCK_TTL_SECONDS = int(os.getenv("REPO_LOCK_TTL_SECONDS", "1800"))

_redis_client = None


def get_redis_client():
    global _redis_client
    if _redis_client is None:
        _redis_client = redis.Redis.from_url(REDIS_URL, decode_responses=True)
    return _redis_client


def _lock_key(repository_id) -> str:
    return f"automaintainer:repo-lock:{repository_id}"


def acquire_repo_lock(repository_id, run_id: str) -> bool:
    """Return True if this run_id now holds the lock, False if another run does."""
    client = get_redis_client()
    return bool(
        client.set(_lock_key(repository_id), run_id, nx=True, ex=LOCK_TTL_SECONDS)
    )


_RELEASE_SCRIPT = """
if redis.call("get", KEYS[1]) == ARGV[1] then
    return redis.call("del", KEYS[1])
else
    return 0
end
"""


def release_repo_lock(repository_id, run_id: str) -> None:
    """Release the lock only if run_id is still the owner (safe against stale releases)."""
    client = get_redis_client()
    client.eval(_RELEASE_SCRIPT, 1, _lock_key(repository_id), run_id)


def is_redis_available() -> bool:
    """
    Cheap reachability check used to decide between queuing on Celery
    vs. running inline. Cached would be nicer for hot paths, but a
    single PING is cheap enough for start/cancel-frequency calls.
    """
    if not os.getenv("REDIS_URL"):
        return False
    try:
        get_redis_client().ping()
        return True
    except Exception:
        return False


def heartbeat_key(run_id: str) -> str:
    return f"automaintainer:heartbeat:{run_id}"


def write_heartbeat(run_id: str, ttl_seconds: int = 60) -> None:
    """Called periodically by a running task to prove it's still alive."""
    try:
        get_redis_client().set(heartbeat_key(run_id), "1", ex=ttl_seconds)
    except Exception:
        # Heartbeat failures shouldn't take down the agent run itself.
        pass


def is_heartbeat_alive(run_id: str) -> bool:
    try:
        return bool(get_redis_client().exists(heartbeat_key(run_id)))
    except Exception:
        # If Redis is unreachable we can't tell -- treat as "don't know",
        # let the DB-timestamp staleness check in cleanup_stale_runs decide.
        return True
