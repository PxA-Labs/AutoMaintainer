from cachelib.bounded import BoundedCache


def test_oldest_entry_is_evicted():
    cache = BoundedCache(2)
    cache.put("a", 1)
    cache.put("b", 2)
    cache.put("c", 3)
    assert "a" not in cache
    assert cache.get("b") == 2
    assert cache.get("c") == 3


def test_get_refreshes_recency():
    cache = BoundedCache(2)
    cache.put("a", 1)
    cache.put("b", 2)
    cache.get("a")
    cache.put("c", 3)
    assert "a" in cache
    assert "b" not in cache


def test_overwrite_refreshes_and_does_not_evict():
    cache = BoundedCache(2)
    cache.put("a", 1)
    cache.put("b", 2)
    cache.put("a", 10)
    assert len(cache) == 2
    assert cache.get("a") == 10
    cache.put("c", 3)
    assert "b" not in cache
    assert "a" in cache
