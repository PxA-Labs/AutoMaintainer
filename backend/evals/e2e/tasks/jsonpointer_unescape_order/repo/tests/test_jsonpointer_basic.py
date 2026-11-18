from jsonpointer import JsonPointer, resolve_pointer


def test_resolve_nested():
    doc = {"foo": {"bar": [10, 20]}}
    assert resolve_pointer(doc, "/foo/bar/1") == 20


def test_escaped_slash():
    assert resolve_pointer({"a/b": 1}, "/a~1b") == 1
    assert JsonPointer.from_parts(["a/b"]).path == "/a~1b"
