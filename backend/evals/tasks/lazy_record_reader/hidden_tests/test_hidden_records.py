import itertools

import pytest

from etl.records import read_records


def _source(*lines, then=None):
    yield from lines
    raise AssertionError(then or "read past the requested records")


def test_returns_an_iterator():
    result = read_records(["a,1"])
    assert iter(result) is result
    assert next(result) == ("a", 1)


def test_reads_only_what_is_needed():
    records = read_records(_source("a,1", "", "b,2"))
    assert list(itertools.islice(records, 2)) == [("a", 1), ("b", 2)]


def test_error_is_raised_when_reached():
    records = read_records(_source("a,1", "b,2", "oops", then="unreachable"))
    assert next(records) == ("a", 1)
    assert next(records) == ("b", 2)
    with pytest.raises(ValueError, match="line 3"):
        next(records)


def test_parsing_unchanged():
    assert list(read_records(["x,-5", "  ", "y , 0"])) == [("x", -5), ("y", 0)]
    with pytest.raises(ValueError, match="line 1"):
        list(read_records(["a,b,c"]))
