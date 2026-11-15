"""Order-preserving de-duplication."""


def unique(items, key=None) -> list:
    """Return ``items`` without duplicates, keeping each first occurrence.

    ``key`` maps an item to the value used to compare items.
    """
    seen = {}
    for item in items:
        seen[key(item) if key else item] = item
    return list(seen.values())
