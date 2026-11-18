import pytest

from boltons.strutils import parse_int_list


@pytest.mark.parametrize(
    "text, expected",
    [
        ("8-5", [5, 6, 7, 8]),
        ("1,4-2", [1, 2, 3, 4]),
        ("10-10", [10]),
        ("3-1,9", [1, 2, 3, 9]),
    ],
)
def test_descending_ranges(text, expected):
    assert parse_int_list(text) == expected


def test_custom_delimiters():
    assert parse_int_list("6:4;1", delim=";", range_delim=":") == [1, 4, 5, 6]
    assert parse_int_list("2-4") == [2, 3, 4]
