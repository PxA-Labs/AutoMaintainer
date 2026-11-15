"""Parse compact human-written durations."""

import re

_PART = re.compile(r"(\d+)([hm])")
_UNIT_SECONDS = {"h": 3600, "m": 60}


def parse_duration(text: str) -> int:
    """Parse a compact duration such as "1h30m" into a number of seconds."""
    total = 0
    for amount, unit in _PART.findall(text):
        total += int(amount) * _UNIT_SECONDS[unit]
    return total
