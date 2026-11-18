import pytest

from boltons.strutils import format_int_list, parse_int_list


@pytest.mark.parametrize(
    "values, expected",
    [
        ([1, 5, 6, 7], "1,5-7"),
        ([2, 3], "2-3"),
        ([4, 1, 2, 3], "1-4"),
        ([9, 10, 10, 11], "9-11"),
    ],
)
def test_trailing_range(values, expected):
    assert format_int_list(values) == expected


def test_round_trip_and_delimiters():
    values = [1, 2, 3, 7, 8, 20, 21, 22]
    assert parse_int_list(format_int_list(values)) == values
    assert format_int_list([1, 2, 5, 6], range_delim=":") == "1:2,5:6"
