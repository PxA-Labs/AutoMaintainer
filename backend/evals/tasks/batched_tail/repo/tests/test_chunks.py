from streams.chunks import batched


def test_exact_multiple():
    assert list(batched(range(4), 2)) == [[0, 1], [2, 3]]


def test_empty_input():
    assert list(batched([], 3)) == []
