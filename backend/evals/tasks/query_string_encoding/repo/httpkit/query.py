"""Helpers for building request URLs."""


def build_query(params: dict) -> str:
    """Return ``params`` as a URL query string (without the leading ``?``)."""
    return "&".join(f"{key}={value}" for key, value in params.items())
