from netutil.backoff import backoff_delays


def test_doubling_from_default_base():
    assert backoff_delays(3) == [1.0, 2.0, 4.0]


def test_custom_base_keyword():
    assert backoff_delays(3, base=0.5) == [0.5, 1.0, 2.0]


def test_zero_attempts():
    assert backoff_delays(0) == []
