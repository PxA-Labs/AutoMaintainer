"""Sliding-window rate limiting for the public API."""

from collections import deque


class RateLimiter:
    """Allow at most ``limit`` calls in any ``window``-second interval."""

    def __init__(self, limit: int, window: float):
        self.limit = limit
        self.window = window
        self._calls: deque[float] = deque()

    def allow(self, now: float) -> bool:
        while self._calls and self._calls[0] <= now - self.window:
            self._calls.popleft()
        if len(self._calls) >= self.limit:
            return False
        self._calls.append(now)
        return True
