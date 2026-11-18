from toolz.dicttoolz import assoc, get_in, update_in


def test_update_in_existing_key():
    assert update_in({"a": 0}, ["a"], lambda x: x + 1) == {"a": 1}


def test_get_in_nested():
    assert get_in(["a", "b"], {"a": {"b": 3}}) == 3
    assert get_in(["a", "c"], {"a": {"b": 3}}, 0) == 0


def test_assoc():
    assert assoc({"x": 1}, "x", 2) == {"x": 2}
