"""Parse compact human-written durations."""

import re

_PART = re.compile(r"(\d+)([hms])")
_WHOLE = re.compile(r"(?:\d+[hms])+")
_UNIT_SECONDS = {"h": 3600, "m": 60, "s": 1}


def parse_duration(text: str) -> int:
    """Parse a compact duration such as "1h30m" or "2m15s" into seconds.

    Raises ValueError unless the whole string is a sequence of
    ``<number><unit>`` parts with unit ``h``, ``m`` or ``s``.
    """
    if not _WHOLE.fullmatch(text):
        raise ValueError(f"invalid duration: {text!r}")
    total = 0
    for amount, unit in _PART.findall(text):
        total += int(amount) * _UNIT_SECONDS[unit]
    return total
