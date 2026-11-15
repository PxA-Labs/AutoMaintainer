"""Retry timing helpers."""


def backoff_delays(attempts: int, base: float = 1.0) -> list[float]:
    """Seconds to wait before each retry: ``base``, ``2*base``, ``4*base``..."""
    return [base * 2**i for i in range(attempts)]
