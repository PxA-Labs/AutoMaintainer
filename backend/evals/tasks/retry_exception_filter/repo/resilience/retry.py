"""Retrying flaky calls to downstream services."""

import functools


def retry(times: int = 3, exceptions: tuple = (Exception,)):
    """Call the decorated function up to ``times`` times.

    Only exceptions that are instances of ``exceptions`` trigger another
    attempt.
    """

    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            for _ in range(times):
                try:
                    return func(*args, **kwargs)
                except Exception:
                    continue
            return None

        return wrapper

    return decorator
