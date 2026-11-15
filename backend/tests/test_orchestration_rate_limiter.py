"""
Rate limiter reliability tests.

Celery runs every task on a fresh event loop while the RateLimitManager is
a process-wide singleton, so these tests deliberately use asyncio.run()
more than once against the same manager.
"""

import asyncio
import os
import sys
import time
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import rate_limiter as rl  # noqa: E402


@pytest.fixture
def clock(monkeypatch):
    """Controllable time.time() for the rate_limiter module."""
    now = [time.time()]
    monkeypatch.setattr(rl, "time", SimpleNamespace(time=lambda: now[0]))
    return now


@pytest.fixture
def manager_env(monkeypatch):
    for i in range(1, 20):
        monkeypatch.delenv(f"GROQ_API_KEY_{i}", raising=False)
    monkeypatch.setenv("GROQ_API_KEY", "k0")
    monkeypatch.setattr(rl, "_rate_limit_manager", None)
    yield
    rl._rate_limit_manager = None


def test_unhealthy_key_recovers_in_a_later_event_loop(manager_env, clock):
    async def task_one():
        m = await rl.get_rate_limit_manager()
        for _ in range(3):
            m.keys[0].record_error(Exception("Error code: 503 - service unavailable"))
        return m

    m = asyncio.run(task_one())  # first Celery task; its loop is now closed
    assert not m.keys[0].is_healthy

    clock[0] += rl.UNHEALTHY_COOLDOWN_SECONDS + 1

    async def task_two():
        return await (await rl.get_rate_limit_manager()).acquire(100, timeout=1)

    key = asyncio.run(task_two())  # second task, brand-new loop
    assert key.name == "primary"
    assert key.is_healthy
    assert key.consecutive_errors == 0


def test_unhealthy_key_stays_benched_during_cooldown(manager_env, clock):
    async def go():
        m = await rl.get_rate_limit_manager()
        for _ in range(3):
            m.keys[0].record_error(Exception("Error code: 429 - rate limit"))
        clock[0] += rl.UNHEALTHY_COOLDOWN_SECONDS / 2
        with pytest.raises(RuntimeError, match="No healthy API keys"):
            await m.acquire(100, timeout=1)

    asyncio.run(go())


@pytest.mark.parametrize(
    "error",
    [
        Exception("Error code: 400 - context_length_exceeded"),
        RuntimeError("MCP tool 'search' failed: gitnexus not found"),
        ValueError("could not parse model output"),
    ],
)
def test_non_key_errors_do_not_count_against_key(error):
    key = rl.APIKeyState(key="k", name="k")
    for _ in range(5):
        assert key.record_error(error) is False
    assert key.is_healthy
    assert key.consecutive_errors == 0


@pytest.mark.parametrize(
    "error, kind",
    [
        (Exception("Error code: 429 - Rate limit reached"), "rate_limit"),
        (SimpleNamespace(status_code=401), "auth"),
        (Exception("Error code: 401 - invalid_api_key"), "auth"),
        (Exception("Error code: 502 - bad gateway"), "server"),
    ],
)
def test_key_errors_are_classified(error, kind):
    assert rl.classify_key_error(error) == kind


def test_rate_limit_error_drains_bucket_and_counts():
    key = rl.APIKeyState(key="k", name="k")
    assert key.record_error(Exception("Error code: 429 - Rate limit reached"))
    assert key.request_tokens == 0
    assert key.consecutive_errors == 1


def test_execute_with_retry_does_not_bench_key_on_prompt_errors(manager_env):
    async def bad_prompt(api_key):
        raise Exception("Error code: 400 - context_length_exceeded")

    async def go():
        m = await rl.get_rate_limit_manager()
        with pytest.raises(Exception, match="400"):
            await m.execute_with_retry(bad_prompt, estimated_tokens=10)
        return m

    m = asyncio.run(go())
    assert m.keys[0].is_healthy


def test_estimate_above_per_minute_budget_fails_fast(manager_env, monkeypatch):
    monkeypatch.setenv("GROQ_TOKENS_PER_MINUTE", "2500")
    m = rl.RateLimitManager()

    async def go():
        await m.initialize()
        return await m.acquire(3000, timeout=2)

    start = time.monotonic()
    with pytest.raises(rl.RequestExceedsBudgetError, match="2500"):
        asyncio.run(go())
    assert time.monotonic() - start < 1


def test_manager_usable_across_event_loops(manager_env):
    async def one_call():
        m = await rl.get_rate_limit_manager()
        return (await m.acquire(10, timeout=1)).name

    assert asyncio.run(one_call()) == "primary"
    assert asyncio.run(one_call()) == "primary"
    assert asyncio.run(rl.close_rate_limit_manager()) is None
