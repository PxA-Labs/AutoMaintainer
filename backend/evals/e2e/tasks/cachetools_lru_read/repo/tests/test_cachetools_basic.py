from cachetools import LRUCache, TTLCache


def test_lru_evicts_oldest_insert():
    cache = LRUCache(maxsize=2)
    cache["a"] = 1
    cache["b"] = 2
    cache["c"] = 3
    assert "a" not in cache and len(cache) == 2


def test_ttl_before_expiry():
    now = [0]
    cache = TTLCache(maxsize=4, ttl=10, timer=lambda: now[0])
    cache["k"] = "v"
    now[0] = 5
    assert cache["k"] == "v"
