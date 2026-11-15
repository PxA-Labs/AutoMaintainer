"""Latency percentiles for the status page."""

import math


def percentile(values: list[float], p: float) -> float:
    """Return the ``p``-th percentile of ``values`` (nearest-rank method).

    The nearest rank is ``ceil(p / 100 * n)`` (at least 1) in the sorted
    values, counting from 1.
    """
    if not values:
        raise ValueError("percentile of an empty list")
    if not 0 <= p <= 100:
        raise ValueError(f"p must be between 0 and 100, got {p}")
    ordered = sorted(values)
    rank = max(1, math.ceil(p / 100 * len(ordered)))
    return ordered[rank - 1]
