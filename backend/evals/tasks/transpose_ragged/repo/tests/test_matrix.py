from grid.matrix import transpose


def test_values_are_transposed():
    result = transpose([[1, 2, 3], [4, 5, 6]])
    assert [list(column) for column in result] == [[1, 4], [2, 5], [3, 6]]


def test_no_rows():
    assert list(transpose([])) == []
