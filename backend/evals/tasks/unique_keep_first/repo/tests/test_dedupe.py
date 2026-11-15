from collectionutil.dedupe import unique


def test_plain_values_keep_order():
    assert unique([3, 1, 3, 2, 1]) == [3, 1, 2]


def test_empty():
    assert unique([]) == []
