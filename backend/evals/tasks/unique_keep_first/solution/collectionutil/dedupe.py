"""Order-preserving de-duplication."""


def unique(items, key=None) -> list:
    """Return ``items`` without duplicates, keeping each first occurrence.

    ``key`` maps an item to the value used to compare items.
    """
    seen = set()
    result = []
    for item in items:
        marker = key(item) if key else item
        if marker not in seen:
            seen.add(marker)
            result.append(item)
    return result
