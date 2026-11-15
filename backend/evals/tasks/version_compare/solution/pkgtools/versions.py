"""Version string helpers."""


def compare_versions(a: str, b: str) -> int:
    """Compare two dotted version strings such as ``"1.4.2"``.

    Returns -1 if ``a`` is older than ``b``, 0 if they are equal and 1 if
    ``a`` is newer. Parts compare numerically; a missing part counts as 0.
    """
    parts_a = [int(part) for part in a.split(".")]
    parts_b = [int(part) for part in b.split(".")]
    width = max(len(parts_a), len(parts_b))
    parts_a += [0] * (width - len(parts_a))
    parts_b += [0] * (width - len(parts_b))
    if parts_a < parts_b:
        return -1
    if parts_a > parts_b:
        return 1
    return 0
