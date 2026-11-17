import pytest

from cachetools import LRUCache


def test_read_protects_from_eviction():
    cache = LRUCache(maxsize=2)
    cache["a"] = 1
    cache["b"] = 2
    assert cache["a"] == 1
    cache["c"] = 3
    assert "a" in cache and "b" not in cache


def test_get_counts_as_use_and_missing_read_changes_nothing():
    cache = LRUCache(maxsize=3)
    for key in "xyz":
        cache[key] = key
    assert cache.get("x") == "x"
    assert cache.get("missing") is None
    with pytest.raises(KeyError):
        cache["missing"]
    cache["w"] = "w"
    assert sorted(cache) == ["w", "x", "z"]
