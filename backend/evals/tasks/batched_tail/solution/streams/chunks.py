"""Chunking helpers for bulk inserts."""


def batched(iterable, size: int):
    """Yield lists of ``size`` consecutive items from ``iterable``.

    The last batch may be shorter.
    """
    if size < 1:
        raise ValueError("size must be at least 1")
    batch = []
    for item in iterable:
        batch.append(item)
        if len(batch) == size:
            yield batch
            batch = []
    if batch:
        yield batch
