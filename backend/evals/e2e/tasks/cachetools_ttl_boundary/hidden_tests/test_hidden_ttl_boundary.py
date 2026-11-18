import pytest

from cachetools import TTLCache


@pytest.mark.parametrize("ttl", [1, 10, 60])
def test_item_expires_exactly_at_ttl(ttl):
    now = [100]
    cache = TTLCache(maxsize=4, ttl=ttl, timer=lambda: now[0])
    cache["k"] = "v"
    now[0] = 100 + ttl
    assert "k" not in cache
    with pytest.raises(KeyError):
        cache["k"]
    assert cache.get("k") is None


def test_item_alive_just_before_expiry():
    now = [0]
    cache = TTLCache(maxsize=4, ttl=10, timer=lambda: now[0])
    cache["k"] = "v"
    now[0] = 9.5
    assert cache["k"] == "v"
