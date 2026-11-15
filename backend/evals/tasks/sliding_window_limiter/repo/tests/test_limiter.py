from throttle.limiter import RateLimiter


def test_allows_up_to_limit_then_rejects():
    limiter = RateLimiter(limit=2, window=10)
    assert limiter.allow(0) is True
    assert limiter.allow(1) is True
    assert limiter.allow(2) is False


def test_single_old_call_expires():
    limiter = RateLimiter(limit=1, window=10)
    assert limiter.allow(0) is True
    assert limiter.allow(10) is True
