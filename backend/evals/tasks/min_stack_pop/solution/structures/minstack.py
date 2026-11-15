"""A stack that also reports its minimum."""


class MinStack:
    def __init__(self):
        self._items = []
        self._mins = []  # _mins[i] is the minimum of _items[: i + 1]

    def __len__(self) -> int:
        return len(self._items)

    def push(self, value) -> None:
        self._items.append(value)
        self._mins.append(value if not self._mins else min(value, self._mins[-1]))

    def pop(self):
        """Remove and return the top value."""
        if not self._items:
            raise IndexError("pop from an empty MinStack")
        self._mins.pop()
        return self._items.pop()

    def min(self):
        """Return the smallest value on the stack."""
        if not self._mins:
            raise IndexError("min of an empty MinStack")
        return self._mins[-1]
