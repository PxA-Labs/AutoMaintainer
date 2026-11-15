"""A stack that also reports its minimum."""


class MinStack:
    def __init__(self):
        self._items = []
        self._min = None

    def __len__(self) -> int:
        return len(self._items)

    def push(self, value) -> None:
        self._items.append(value)
        if self._min is None or value < self._min:
            self._min = value

    def pop(self):
        """Remove and return the top value."""
        return self._items.pop()

    def min(self):
        """Return the smallest value on the stack."""
        return self._min
