"""Retry timing helpers."""


def backoff_delays(
    attempts: int, base: float = 1.0, max_delay: float | None = None
) -> list[float]:
    """Seconds to wait before each retry: ``base``, ``2*base``, ``4*base``...

    With ``max_delay`` set, no delay is longer than ``max_delay``.
    """
    if max_delay is not None and max_delay <= 0:
        raise ValueError("max_delay must be positive")
    delays = [base * 2**i for i in range(attempts)]
    if max_delay is None:
        return delays
    return [min(delay, max_delay) for delay in delays]
