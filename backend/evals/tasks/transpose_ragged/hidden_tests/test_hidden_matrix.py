import pytest

from grid.matrix import transpose


def test_returns_lists():
    assert transpose([[1, 2, 3], [4, 5, 6]]) == [[1, 4], [2, 5], [3, 6]]
    assert transpose([["a"]]) == [["a"]]


@pytest.mark.parametrize("rows", [[[1, 2], [3]], [[1], [2, 3]], [[1, 2], [], [3, 4]]])
def test_ragged_rows_raise(rows):
    with pytest.raises(ValueError):
        transpose(rows)


def test_input_is_not_modified_and_result_is_new():
    rows = [[1, 2], [3, 4]]
    result = transpose(rows)
    result[0][0] = 99
    assert rows == [[1, 2], [3, 4]]
    assert result == [[99, 3], [2, 4]]
    assert transpose(result) == [[99, 2], [3, 4]]
