import xmltodict


def test_parse_simple_document():
    assert xmltodict.parse("<a><b>1</b><c>x</c></a>") == {"a": {"b": "1", "c": "x"}}


def test_attributes():
    assert xmltodict.parse('<a href="x">t</a>') == {"a": {"@href": "x", "#text": "t"}}
