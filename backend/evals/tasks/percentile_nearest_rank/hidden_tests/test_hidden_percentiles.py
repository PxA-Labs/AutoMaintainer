import pytest

from metrics.percentiles import percentile

DATA = [15, 20, 35, 40, 50]


@pytest.mark.parametrize(
    "p, expected", [(0, 15), (5, 15), (40, 20), (80, 40), (100, 50)]
)
def test_nearest_rank(p, expected):
    assert percentile(DATA, p) == expected


def test_unsorted_input_is_sorted_first():
    assert percentile([50, 15, 40, 20, 35], 40) == 20
    assert percentile([3, 1, 2], 100) == 3


def test_input_list_is_not_modified():
    values = [3, 1, 2]
    percentile(values, 50)
    assert values == [3, 1, 2]


@pytest.mark.parametrize("p", [-1, 100.5, 250])
def test_out_of_range_p_raises(p):
    with pytest.raises(ValueError):
        percentile(DATA, p)


def test_empty_list_raises_value_error():
    with pytest.raises(ValueError):
        percentile([], 50)
