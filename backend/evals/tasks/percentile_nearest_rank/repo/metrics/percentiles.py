"""Latency percentiles for the status page."""


def percentile(values: list[float], p: float) -> float:
    """Return the ``p``-th percentile of ``values`` (nearest-rank method).

    The nearest rank is ``ceil(p / 100 * n)`` (at least 1) in the sorted
    values, counting from 1.
    """
    index = int(p / 100 * len(values))
    return values[index]
