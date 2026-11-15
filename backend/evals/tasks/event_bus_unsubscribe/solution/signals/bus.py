"""In-process publish/subscribe."""

from collections import defaultdict


class EventBus:
    def __init__(self):
        self._handlers = defaultdict(list)

    def subscribe(self, event: str, handler):
        """Call ``handler(payload)`` whenever ``event`` is published.

        Returns a function that removes this subscription (idempotent).
        """
        entry = [handler]  # a unique token, even for the same handler twice
        self._handlers[event].append(entry)

        def unsubscribe() -> None:
            entries = self._handlers.get(event, [])
            for i, existing in enumerate(entries):
                if existing is entry:
                    del entries[i]
                    return

        return unsubscribe

    def publish(self, event: str, payload=None) -> int:
        """Call every handler of ``event`` in subscription order.

        Returns the number of handlers called.
        """
        entries = list(self._handlers.get(event, []))
        for (handler,) in entries:
            handler(payload)
        return len(entries)
