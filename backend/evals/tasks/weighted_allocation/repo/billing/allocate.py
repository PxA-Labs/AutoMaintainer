"""Splitting invoice amounts between cost centres."""


def split_evenly(total_cents: int, parts: int) -> list[int]:
    """Split ``total_cents`` into ``parts`` shares that differ by at most 1.

    Earlier shares receive the extra cents.
    """
    if parts <= 0:
        raise ValueError("parts must be positive")
    base, extra = divmod(total_cents, parts)
    return [base + 1 if i < extra else base for i in range(parts)]
