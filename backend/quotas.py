"""
Request quotas for the inline assist endpoint (#402).

Every /assist/inline call spends LLM tokens on the server's Groq keys, so it
is limited per user and per organization:

- ``ASSIST_RATE_PER_MINUTE``        requests per user per minute (default 20)
- ``ASSIST_ORG_RATE_PER_MINUTE``    requests per org per minute (default 60)
- ``ASSIST_DAILY_REQUESTS_PER_ORG`` requests per org per UTC day (default 1000)

A value of 0 disables that limit.

Algorithm: fixed windows. The counter key embeds the window number
(``int(now // window)``), so a window resets on its own and the daily window
lines up with UTC midnight. A fixed window can admit up to twice the limit
across a window boundary; for an abuse guard that is an acceptable price for
one INCR per check (a token bucket needs a Lua script per call).

The budget counts requests, not tokens: the token count of a streamed answer
is only known after the response has been sent, and the per-key token limits
are already enforced by rate_limiter.py.

Storage: Redis when ``REDIS_URL`` is set, so every API process shares the
counters. Without Redis, or while Redis is failing, counters fall back to
this process's memory. **Caveat:** in-memory counters are per process, so
with N uvicorn workers or replicas the effective limit is N times the
configured one, and they reset on restart. That is a deliberate fail-open:
an unavailable Redis should not take inline assist down with it.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

logger = logging.getLogger(__name__)

KEY_PREFIX = "automaintainer:quota"
MINUTE = 60
DAY = 86_400

# After a Redis error, use memory for this long before trying Redis again,
# so a dead Redis does not add a connect timeout to every request.
REDIS_RETRY_AFTER_SECONDS = 30.0
REDIS_SOCKET_TIMEOUT_SECONDS = 0.5


def _env_int(name: str, default: int) -> int:
    try:
        return max(0, int(os.getenv(name, default)))
    except ValueError:
        logger.warning("Ignoring non-integer %s; using %s", name, default)
        return default


@dataclass(frozen=True)
class QuotaExceeded:
    """Which limit was hit and how long until its window resets."""

    scope: str
    limit: int
    retry_after: int


class MemoryCounter:
    """Fixed-window counters in this process only (see module caveat)."""

    def __init__(self):
        self._lock = threading.Lock()
        self._counts: dict[str, tuple[float, int]] = {}

    def incr(self, key: str, ttl: int, now: float) -> int:
        with self._lock:
            # Drop expired windows so the dict cannot grow without bound.
            if len(self._counts) > 10_000:
                self._counts = {k: v for k, v in self._counts.items() if v[0] > now}
            expires_at, count = self._counts.get(key, (0.0, 0))
            if expires_at <= now:
                expires_at, count = now + ttl, 0
            count += 1
            self._counts[key] = (expires_at, count)
            return count


class RedisCounter:
    def __init__(self, client):
        self.client = client

    def incr(self, key: str, ttl: int, now: float) -> int:
        pipe = self.client.pipeline()
        pipe.incr(key)
        pipe.expire(key, ttl)
        count, _ = pipe.execute()
        return int(count)


def _default_redis_client():
    url = os.getenv("REDIS_URL")
    if not url:
        return None
    import redis

    return redis.Redis.from_url(
        url,
        socket_timeout=REDIS_SOCKET_TIMEOUT_SECONDS,
        socket_connect_timeout=REDIS_SOCKET_TIMEOUT_SECONDS,
    )


class QuotaLimiter:
    def __init__(
        self,
        redis_factory: Callable[[], Optional[object]] = _default_redis_client,
        clock: Callable[[], float] = time.time,
    ):
        self._redis_factory = redis_factory
        self._redis: Optional[RedisCounter] = None
        self._redis_down_until = 0.0
        self._memory = MemoryCounter()
        self._clock = clock

    def _incr(self, key: str, ttl: int, now: float) -> int:
        if now >= self._redis_down_until:
            try:
                if self._redis is None:
                    client = self._redis_factory()
                    if client is not None:
                        self._redis = RedisCounter(client)
                if self._redis is not None:
                    return self._redis.incr(key, ttl, now)
            except Exception as exc:
                logger.warning(
                    "Quota store unavailable, using per-process counters: %s", exc
                )
                self._redis_down_until = now + REDIS_RETRY_AFTER_SECONDS
        return self._memory.incr(key, ttl, now)

    def hit(self, scope: str, ident: str, limit: int, window: int):
        """Count one request; return QuotaExceeded if it is over ``limit``."""
        if limit <= 0:
            return None
        now = self._clock()
        window_no = int(now // window)
        key = f"{KEY_PREFIX}:{scope}:{ident}:{window}:{window_no}"
        # A minute of slack so a key outlives its window across clock skew.
        count = self._incr(key, window + MINUTE, now)
        if count <= limit:
            return None
        retry_after = max(1, int((window_no + 1) * window - now + 0.999))
        return QuotaExceeded(scope=scope, limit=limit, retry_after=retry_after)

    def check_assist(self, user_id: str, org_id: str) -> Optional[QuotaExceeded]:
        """Apply the inline-assist limits, narrowest first.

        Checking stops at the first exceeded limit, so a request that a user
        limit rejects does not also eat into the org's daily budget.
        """
        checks = (
            ("user-minute", user_id, _env_int("ASSIST_RATE_PER_MINUTE", 20), MINUTE),
            (
                "org-minute",
                org_id,
                _env_int("ASSIST_ORG_RATE_PER_MINUTE", 60),
                MINUTE,
            ),
            (
                "org-day",
                org_id,
                _env_int("ASSIST_DAILY_REQUESTS_PER_ORG", 1000),
                DAY,
            ),
        )
        for scope, ident, limit, window in checks:
            exceeded = self.hit(scope, ident, limit, window)
            if exceeded:
                return exceeded
        return None


assist_limiter = QuotaLimiter()
