"""Splitting invoice amounts between cost centres."""


def split_evenly(total_cents: int, parts: int) -> list[int]:
    """Split ``total_cents`` into ``parts`` shares that differ by at most 1.

    Earlier shares receive the extra cents.
    """
    if parts <= 0:
        raise ValueError("parts must be positive")
    base, extra = divmod(total_cents, parts)
    return [base + 1 if i < extra else base for i in range(parts)]


def allocate(total_cents: int, weights: list[int]) -> list[int]:
    """Split ``total_cents`` in proportion to ``weights`` (largest remainder)."""
    if total_cents < 0:
        raise ValueError("total_cents must not be negative")
    if not weights:
        raise ValueError("weights must not be empty")
    if any(w < 0 for w in weights):
        raise ValueError("weights must not be negative")
    weight_sum = sum(weights)
    if weight_sum == 0:
        raise ValueError("at least one weight must be positive")
    shares, remainders = [], []
    for w in weights:
        share, rem = divmod(total_cents * w, weight_sum)
        shares.append(share)
        remainders.append(rem)
    leftover = total_cents - sum(shares)
    order = sorted(range(len(weights)), key=lambda i: (-remainders[i], i))
    for i in order[:leftover]:
        shares[i] += 1
    return shares
