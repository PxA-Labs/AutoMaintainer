from throttle.limiter import RateLimiter


def test_whole_burst_expires_after_the_window():
    limiter = RateLimiter(limit=3, window=10)
    assert [limiter.allow(t) for t in (0, 1, 2)] == [True, True, True]
    assert limiter.allow(3) is False
    assert [limiter.allow(t) for t in (50, 50.5, 51)] == [True, True, True]
    assert limiter.allow(51.5) is False


def test_rejected_calls_do_not_count():
    limiter = RateLimiter(limit=2, window=10)
    assert limiter.allow(0) is True
    assert limiter.allow(5) is True
    for t in (6, 7, 8, 9):
        assert limiter.allow(t) is False
    # The call at 0 has expired; only the accepted call at 5 is in (0, 10].
    assert limiter.allow(10) is True
    assert limiter.allow(10.5) is False
    # Both accepted calls (5 and 10) have expired by 20.
    assert limiter.allow(20) is True


def test_boundary_is_half_open():
    limiter = RateLimiter(limit=1, window=5)
    assert limiter.allow(1) is True
    assert limiter.allow(5.999) is False
    assert limiter.allow(6) is True
