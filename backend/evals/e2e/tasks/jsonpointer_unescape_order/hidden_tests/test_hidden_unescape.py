import pytest

from jsonpointer import JsonPointer, resolve_pointer


def test_tilde_zero_one_is_a_literal_tilde_one():
    doc = {"a~1b": 1, "a/b": 2}
    assert resolve_pointer(doc, "/a~01b") == 1
    assert JsonPointer("/~01").parts == ["~1"]


@pytest.mark.parametrize(
    "parts", [["~1"], ["a~1b", "c/d"], ["~", "/"], ["~01", "x~0y"]]
)
def test_from_parts_round_trips(parts):
    assert JsonPointer(JsonPointer.from_parts(parts).path).parts == parts
