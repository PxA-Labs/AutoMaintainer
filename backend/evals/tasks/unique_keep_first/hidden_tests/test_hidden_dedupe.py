from collectionutil.dedupe import unique


def test_key_keeps_first_of_each_group():
    assert unique(["a", "B", "b", "A", "c"], key=str.lower) == ["a", "B", "c"]


def test_records_by_id_keep_first_record():
    rows = [
        {"id": 2, "v": "first-2"},
        {"id": 1, "v": "first-1"},
        {"id": 2, "v": "second-2"},
        {"id": 1, "v": "second-1"},
    ]
    assert [r["v"] for r in unique(rows, key=lambda r: r["id"])] == [
        "first-2",
        "first-1",
    ]


def test_accepts_generator():
    assert unique((n % 3 for n in range(10))) == [0, 1, 2]
    assert unique((w for w in ["x", "X", "y"]), key=str.upper) == ["x", "y"]
