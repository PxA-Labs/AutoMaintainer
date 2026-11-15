from scheduling.intervals import merge_intervals


def test_sorted_overlapping_input():
    assert merge_intervals([(1, 4), (2, 6), (8, 9)]) == [(1, 6), (8, 9)]


def test_empty_input():
    assert merge_intervals([]) == []
