import pytest

from netutil.backoff import backoff_delays


def test_delays_are_clamped():
    assert backoff_delays(5, 1.0, max_delay=5.0) == [1.0, 2.0, 4.0, 5.0, 5.0]


def test_none_means_no_limit():
    assert backoff_delays(4, base=2.0, max_delay=None) == [2.0, 4.0, 8.0, 16.0]


@pytest.mark.parametrize("bad", [0, -1.5])
def test_non_positive_cap_raises(bad):
    with pytest.raises(ValueError):
        backoff_delays(3, max_delay=bad)
