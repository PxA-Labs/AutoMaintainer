"""Version string helpers."""


def compare_versions(a: str, b: str) -> int:
    """Compare two dotted version strings such as ``"1.4.2"``.

    Returns -1 if ``a`` is older than ``b``, 0 if they are equal and 1 if
    ``a`` is newer.
    """
    parts_a = a.split(".")
    parts_b = b.split(".")
    if parts_a < parts_b:
        return -1
    if parts_a > parts_b:
        return 1
    return 0
