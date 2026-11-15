from httpkit.query import build_query


def test_values_are_percent_encoded():
    assert build_query({"q": "fish & chips"}) == "q=fish%20%26%20chips"
    assert build_query({"path": "a/b=c"}) == "path=a%2Fb%3Dc"


def test_keys_are_encoded_too():
    assert build_query({"a b": "1"}) == "a%20b=1"


def test_none_values_are_skipped():
    assert build_query({"a": 1, "b": None, "c": "x"}) == "a=1&c=x"


def test_list_values_repeat_the_key():
    assert build_query({"tag": ["red", "big one"], "n": 3}) == (
        "tag=red&tag=big%20one&n=3"
    )
    assert build_query({"tag": ("x",)}) == "tag=x"


def test_empty_list_adds_nothing():
    assert build_query({"tag": [], "page": 1}) == "page=1"
