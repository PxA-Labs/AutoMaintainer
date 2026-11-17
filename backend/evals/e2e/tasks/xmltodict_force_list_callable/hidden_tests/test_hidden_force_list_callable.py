import xmltodict


def test_callable_force_list():
    def only_b(path, key, value):
        return key == "b"

    assert xmltodict.parse("<a><b>1</b><c>2</c></a>", force_list=only_b) == {
        "a": {"b": ["1"], "c": "2"}
    }


def test_callable_receives_parent_path():
    seen = []

    def record(path, key, value):
        seen.append(([name for name, _ in path], key))
        return False

    xmltodict.parse("<a><b><c>x</c></b></a>", force_list=record)
    assert (["a", "b"], "c") in seen
    assert (["a"], "b") in seen


def test_tuple_form_still_works():
    assert xmltodict.parse("<a><b>1</b></a>", force_list=("b",)) == {"a": {"b": ["1"]}}
