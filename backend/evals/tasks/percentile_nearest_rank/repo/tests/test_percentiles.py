from metrics.percentiles import percentile


def test_median_of_sorted_odd_list():
    assert percentile([15, 20, 35, 40, 50], 50) == 35


def test_single_value():
    assert percentile([7], 30) == 7
