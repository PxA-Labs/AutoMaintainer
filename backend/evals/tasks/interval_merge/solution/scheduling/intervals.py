"""Interval arithmetic for booking calendars."""


def merge_intervals(intervals: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Merge overlapping closed intervals.

    Takes ``(start, end)`` tuples in any order and returns a sorted list of
    disjoint intervals. Intervals that touch, such as ``(1, 3)`` and
    ``(3, 5)``, are merged as well.
    """
    merged: list[tuple[int, int]] = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged
