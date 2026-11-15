from scheduling.intervals import merge_intervals


def test_unsorted_input():
    assert merge_intervals([(8, 9), (2, 6), (1, 4)]) == [(1, 6), (8, 9)]


def test_touching_intervals_merge():
    assert merge_intervals([(1, 3), (3, 5)]) == [(1, 5)]


def test_contained_interval_is_absorbed():
    assert merge_intervals([(5, 6), (0, 10), (12, 13)]) == [(0, 10), (12, 13)]
