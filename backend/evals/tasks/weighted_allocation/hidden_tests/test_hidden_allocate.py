import pytest

from billing.allocate import allocate


def test_equal_weights_tie_break_by_index():
    assert allocate(100, [1, 1, 1]) == [34, 33, 33]


def test_largest_remainder_wins():
    # 10 * 1/3 = 3.33 and 10 * 2/3 = 6.67: the leftover cent goes to index 1.
    assert allocate(10, [1, 2]) == [3, 7]
    # 7 split 2:3:5 -> 1.4, 2.1, 3.5 -> floors 1, 2, 3 and leftover 1 to index 2.
    assert allocate(7, [2, 3, 5]) == [1, 2, 4]


def test_zero_weight_gets_nothing():
    assert allocate(5, [0, 1, 0, 1]) == [0, 3, 0, 2]


@pytest.mark.parametrize(
    "total, weights",
    [(0, [1, 2]), (1, [3, 3, 3]), (999, [7, 11, 13]), (10**12 + 1, [1, 1, 1])],
)
def test_shares_always_add_up(total, weights):
    shares = allocate(total, weights)
    assert sum(shares) == total
    assert len(shares) == len(weights)


@pytest.mark.parametrize(
    "total, weights", [(-1, [1]), (10, []), (10, [1, -1]), (10, [0, 0])]
)
def test_invalid_input_raises(total, weights):
    with pytest.raises(ValueError):
        allocate(total, weights)
