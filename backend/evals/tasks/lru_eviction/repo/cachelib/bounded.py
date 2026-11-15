"""A small in-memory cache with a fixed capacity."""

from collections import OrderedDict


class BoundedCache:
    """A fixed-capacity mapping that evicts the least recently used entry."""

    def __init__(self, capacity: int):
        if capacity < 1:
            raise ValueError("capacity must be at least 1")
        self.capacity = capacity
        self._data: OrderedDict = OrderedDict()

    def get(self, key, default=None):
        return self._data.get(key, default)

    def put(self, key, value) -> None:
        self._data[key] = value
        if len(self._data) > self.capacity:
            self._data.popitem(last=True)

    def __len__(self) -> int:
        return len(self._data)

    def __contains__(self, key) -> bool:
        return key in self._data
