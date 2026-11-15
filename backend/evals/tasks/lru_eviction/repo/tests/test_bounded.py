import pytest

from cachelib.bounded import BoundedCache


def test_capacity_must_be_positive():
    with pytest.raises(ValueError):
        BoundedCache(0)


def test_get_returns_stored_value_or_default():
    cache = BoundedCache(2)
    cache.put("a", 1)
    assert cache.get("a") == 1
    assert cache.get("missing", "d") == "d"


def test_size_never_exceeds_capacity():
    cache = BoundedCache(2)
    for i in range(5):
        cache.put(i, i)
    assert len(cache) == 2
