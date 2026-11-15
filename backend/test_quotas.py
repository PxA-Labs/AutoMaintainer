"""Inline-assist quotas (#402): fixed windows, Redis with in-memory fallback,
and the 429 + Retry-After response."""

import json

import pytest
from fastapi.testclient import TestClient
from unittest.mock import patch

import main
import quotas
from main import app
from quotas import QuotaLimiter


class FakeClock:
    def __init__(self, now=1_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


class FakeRedis:
    """Just enough of redis-py's pipeline for RedisCounter."""

    def __init__(self, fail=False):
        self.store = {}
        self.ttls = {}
        self.fail = fail

    def pipeline(self):
        redis = self

        class Pipe:
            def __init__(self):
                self.ops = []

            def incr(self, key):
                self.ops.append(("incr", key))

            def expire(self, key, ttl):
                self.ops.append(("expire", key, ttl))

            def execute(self):
                if redis.fail:
                    raise ConnectionError("redis down")
                out = []
                for op in self.ops:
                    if op[0] == "incr":
                        redis.store[op[1]] = redis.store.get(op[1], 0) + 1
                        out.append(redis.store[op[1]])
                    else:
                        redis.ttls[op[1]] = op[2]
                        out.append(True)
                return out

        return Pipe()


@pytest.fixture
def limits(monkeypatch):
    monkeypatch.setenv("ASSIST_RATE_PER_MINUTE", "2")
    monkeypatch.setenv("ASSIST_ORG_RATE_PER_MINUTE", "3")
    monkeypatch.setenv("ASSIST_DAILY_REQUESTS_PER_ORG", "5")


def test_user_limit_rejects_with_time_to_window_end(limits):
    clock = FakeClock(now=60 * 1000 + 15)  # 15s into a minute window
    limiter = QuotaLimiter(redis_factory=lambda: None, clock=clock)

    assert limiter.check_assist("u1", "o1") is None
    assert limiter.check_assist("u1", "o1") is None
    exceeded = limiter.check_assist("u1", "o1")

    assert exceeded.scope == "user-minute"
    assert exceeded.limit == 2
    assert exceeded.retry_after == 45


def test_user_window_resets_next_minute(limits):
    clock = FakeClock(now=60 * 1000)
    limiter = QuotaLimiter(redis_factory=lambda: None, clock=clock)
    for _ in range(2):
        limiter.check_assist("u1", "o1")
    assert limiter.check_assist("u1", "o1") is not None

    clock.now += 60

    assert limiter.check_assist("u1", "o1") is None


def test_org_limit_spans_users(limits):
    limiter = QuotaLimiter(redis_factory=lambda: None, clock=FakeClock())

    for user in ("u1", "u2", "u3"):
        assert limiter.check_assist(user, "o1") is None
    exceeded = limiter.check_assist("u4", "o1")

    assert exceeded.scope == "org-minute"
    # Another org is unaffected.
    assert limiter.check_assist("u5", "o2") is None


def test_daily_org_budget_retry_after_points_at_utc_midnight(limits, monkeypatch):
    monkeypatch.setenv("ASSIST_ORG_RATE_PER_MINUTE", "0")  # disabled
    day_start = 86_400 * 20_000
    clock = FakeClock(now=day_start + 86_400 - 100)
    limiter = QuotaLimiter(redis_factory=lambda: None, clock=clock)

    for i in range(5):
        assert limiter.check_assist(f"u{i}", "o1") is None
    exceeded = limiter.check_assist("u9", "o1")

    assert exceeded.scope == "org-day"
    assert exceeded.retry_after == 100


def test_zero_disables_every_limit(monkeypatch):
    for name in (
        "ASSIST_RATE_PER_MINUTE",
        "ASSIST_ORG_RATE_PER_MINUTE",
        "ASSIST_DAILY_REQUESTS_PER_ORG",
    ):
        monkeypatch.setenv(name, "0")
    limiter = QuotaLimiter(redis_factory=lambda: None, clock=FakeClock())

    assert all(limiter.check_assist("u1", "o1") is None for _ in range(100))


def test_user_rejection_does_not_spend_org_budget(limits):
    limiter = QuotaLimiter(redis_factory=lambda: None, clock=FakeClock())
    for _ in range(10):
        limiter.check_assist("u1", "o1")

    # u1 was rejected 8 times by the user limit; the org has spent only 2 of 3.
    assert limiter.check_assist("u2", "o1") is None


def test_counters_are_shared_through_redis(limits):
    redis = FakeRedis()
    clock = FakeClock()
    first = QuotaLimiter(redis_factory=lambda: redis, clock=clock)
    second = QuotaLimiter(redis_factory=lambda: redis, clock=clock)

    assert first.check_assist("u1", "o1") is None
    assert second.check_assist("u1", "o1") is None
    assert first.check_assist("u1", "o1").scope == "user-minute"
    # Keys expire a little after their window.
    assert set(redis.ttls.values()) <= {120, 86_460}


def test_redis_failure_falls_back_to_memory_and_backs_off(limits):
    redis = FakeRedis(fail=True)
    calls = []

    def factory():
        calls.append(1)
        return redis

    clock = FakeClock()
    limiter = QuotaLimiter(redis_factory=factory, clock=clock)

    assert limiter.check_assist("u1", "o1") is None
    assert limiter.check_assist("u1", "o1") is None
    assert limiter.check_assist("u1", "o1").scope == "user-minute"
    assert redis.store == {}

    # Redis recovers; it is only retried after the back-off.
    redis.fail = False
    clock.now += quotas.REDIS_RETRY_AFTER_SECONDS + 1
    assert limiter.check_assist("u2", "o2") is None
    assert redis.store


def _payload():
    return {
        "file_path": "a.py",
        "prompt": "p",
        "selected_code": "x",
        "selection": {"startLine": 1, "startColumn": 1, "endLine": 1, "endColumn": 2},
    }


def test_inline_assist_returns_429_with_retry_after(limits, monkeypatch):
    async def stream(**_kwargs):
        yield f"data: {json.dumps({'content': 'ok'})}\n\n"

    monkeypatch.setattr(
        main,
        "assist_limiter",
        QuotaLimiter(redis_factory=lambda: None, clock=FakeClock(now=60 * 1000)),
    )
    app.dependency_overrides[main.get_current_user] = lambda: {
        "user_id": "u1",
        "org_id": "o1",
        "email": "e",
    }
    client = TestClient(app)
    try:
        with patch("agents.stream_inline_assist", side_effect=stream) as assist:
            statuses = [
                client.post("/assist/inline", json=_payload()) for _ in range(3)
            ]
    finally:
        app.dependency_overrides.clear()

    assert [r.status_code for r in statuses] == [200, 200, 429]
    assert statuses[2].headers["Retry-After"] == "60"
    assert "quota" in statuses[2].json()["detail"]
    assert assist.call_count == 2
