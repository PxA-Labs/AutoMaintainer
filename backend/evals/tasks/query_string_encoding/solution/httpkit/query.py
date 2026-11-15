"""Helpers for building request URLs."""

from urllib.parse import quote


def build_query(params: dict) -> str:
    """Return ``params`` as a URL query string (without the leading ``?``)."""
    pairs = []
    for key, value in params.items():
        if value is None:
            continue
        values = value if isinstance(value, (list, tuple)) else [value]
        for item in values:
            pairs.append(f"{quote(str(key), safe='')}={quote(str(item), safe='')}")
    return "&".join(pairs)
