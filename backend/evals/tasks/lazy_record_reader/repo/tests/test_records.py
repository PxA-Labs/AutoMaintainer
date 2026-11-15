import pytest

from etl.records import read_records


def test_parses_and_skips_blank_lines():
    assert list(read_records(["a, 1", "", " b ,2 "])) == [("a", 1), ("b", 2)]


def test_bad_amount_mentions_line():
    with pytest.raises(ValueError, match="line 2"):
        list(read_records(["a,1", "b,x"]))
