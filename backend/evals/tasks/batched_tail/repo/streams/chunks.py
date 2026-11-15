"""Chunking helpers for bulk inserts."""


def batched(iterable, size: int):
    """Yield lists of ``size`` consecutive items from ``iterable``."""
    batch = []
    for item in iterable:
        batch.append(item)
        if len(batch) == size:
            yield batch
            batch = []
