import pytest

from streams.chunks import batched


def test_partial_last_batch_is_kept():
    assert list(batched(range(5), 2)) == [[0, 1], [2, 3], [4]]
    assert list(batched("abcdefg", 3)) == [["a", "b", "c"], ["d", "e", "f"], ["g"]]


def test_size_larger_than_input():
    assert list(batched([1, 2], 10)) == [[1, 2]]


def test_one_shot_iterator():
    assert list(batched(iter(range(3)), 2)) == [[0, 1], [2]]


@pytest.mark.parametrize("size", [0, -2])
def test_invalid_size(size):
    with pytest.raises(ValueError):
        list(batched([1, 2, 3], size))
