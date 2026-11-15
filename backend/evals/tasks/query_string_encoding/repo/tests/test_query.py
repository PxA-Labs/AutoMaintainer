from httpkit.query import build_query


def test_plain_pairs_keep_order():
    assert build_query({"page": 2, "sort": "name"}) == "page=2&sort=name"


def test_empty_params():
    assert build_query({}) == ""
