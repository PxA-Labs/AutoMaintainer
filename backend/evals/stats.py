"""Small-sample statistics for resolve rates (stdlib only)."""

from __future__ import annotations

from math import comb, sqrt

Z_95 = 1.959963984540054  # two-sided 95% normal quantile


def wilson_interval(successes: int, n: int, z: float = Z_95) -> tuple[float, float]:
    """Wilson score interval for a binomial proportion.

    Preferred over the normal (Wald) interval because it stays inside [0, 1]
    and keeps sensible coverage for small n and for rates near 0 or 1, which
    is exactly the regime of a ~10-task eval. ``n == 0`` gives the
    uninformative interval (0, 1).
    """
    if n < 0 or not 0 <= successes <= n:
        raise ValueError(f"need 0 <= successes <= n, got {successes}/{n}")
    if n == 0:
        return (0.0, 1.0)
    p = successes / n
    z2 = z * z
    denom = 1 + z2 / n
    centre = (p + z2 / (2 * n)) / denom
    half = z * sqrt(p * (1 - p) / n + z2 / (4 * n * n)) / denom
    # At k == 0 / k == n the exact bound is 0 / 1; pin it so rounding error
    # cannot exclude the point estimate.
    lo = 0.0 if successes == 0 else max(0.0, centre - half)
    hi = 1.0 if successes == n else min(1.0, centre + half)
    return (lo, hi)


def mcnemar_exact_p(b: int, c: int) -> float:
    """Two-sided exact McNemar p-value from the discordant pair counts.

    ``b``: tasks only system A resolved; ``c``: tasks only B resolved. Under
    H0 (no difference) each discordant task is a fair coin flip, so the test
    is a binomial test on min(b, c) out of b + c.
    """
    n = b + c
    if n == 0:
        return 1.0
    tail = sum(comb(n, k) for k in range(min(b, c) + 1)) / 2**n
    return min(1.0, 2 * tail)
