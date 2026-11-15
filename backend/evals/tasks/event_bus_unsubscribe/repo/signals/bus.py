"""In-process publish/subscribe."""

from collections import defaultdict


class EventBus:
    def __init__(self):
        self._handlers = defaultdict(list)

    def subscribe(self, event: str, handler) -> None:
        """Call ``handler(payload)`` whenever ``event`` is published."""
        self._handlers[event].append(handler)

    def publish(self, event: str, payload=None) -> int:
        """Call every handler of ``event`` in subscription order.

        Returns the number of handlers called.
        """
        handlers = self._handlers.get(event, [])
        for handler in handlers:
            handler(payload)
        return len(handlers)
